"""Read-only, local-first troubleshooting for Jenkins builds."""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_LOG = 256 * 1024
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RULES = [
    ("git_auth", r"authentication failed|permission denied \(publickey\)|could not read username",
     "Check the SCM credential selected by the job, its repository permissions, and whether it expired."),
    ("agent_unavailable", r"agent.*offline|there are no nodes with the label|waiting for next available executor|channelclosedexception",
     "Check the job's agent label, online executors, and agent connectivity in Jenkins."),
    ("docker_daemon", r"cannot connect to the docker daemon|permission denied.*docker\.sock",
     "Check the Docker daemon and the Jenkins agent service account's access to the socket."),
    ("registry_auth", r"pull access denied|no basic auth credentials|unauthorized: authentication required",
     "Confirm the image name and tag, and check registry pull permissions for the agent's credential."),
    ("disk_full", r"no space left on device|disk quota exceeded",
     "Check free disk space, inode use, workspace size, and the agent's Docker build cache."),
    ("out_of_memory", r"oomkilled|outofmemoryerror|cannot allocate memory",
     "Check agent memory pressure and container limits before changing memory or build parallelism."),
    ("process_killed", r"exit code 137|exit status 137",
     "The process received SIGKILL. Check for OOM events, timeouts, and manual termination."),
    ("test_failure", r"tests run:.*failures: [1-9]|there are test failures|=+.*[1-9]\d* failed",
     "Open the test report and reproduce the first failing test using the same dependency versions."),
    ("pipeline_syntax", r"multiplecompilationerrorsexception|no such dsl method|workflowScript:.*expecting",
     "Check the Jenkinsfile line number and verify that the referenced step and plugin exist."),
    ("missing_tool", r"command not found|is not recognized as an internal|cannot run program.*no such file",
     "Check the tool installation and PATH on the actual agent or container executing the step."),
    ("tls_failure", r"pkix path building failed|certificate verify failed|unable to get local issuer certificate",
     "Check the certificate chain and configure the trusted CA on the Jenkins agent."),
    ("dependency_resolution", r"could not resolve dependencies|no matching distribution found|could not find artifact",
     "Check the dependency version, mirror reachability, and repository authentication from the build agent."),
]
RULES = [(name, re.compile(pattern, re.I), action) for name, pattern, action in RULES]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(base, *, local_only=False):
    parsed = urlsplit(base)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or (parsed.scheme == "http" and not loopback)
            or (local_only and not loopback)):
        raise ValueError("Use an HTTPS Jenkins URL or a loopback HTTP URL. Ollama requires loopback.")
    return base.rstrip("/")


def request(url, *, headers=None, payload=None, limit=MAX_LOG, timeout=15, local=False):
    opener = build_opener(ProxyHandler({}), NoRedirect()) if local else build_opener(NoRedirect())
    req = Request(url, headers=headers or {}, data=payload)
    with opener.open(req, timeout=timeout) as response:
        data = response.read(limit + 1)
        if response.headers.get("Content-Type", "").startswith("text/html"):
            raise ValueError("Expected Jenkins API data but received HTML. Check URL and authentication.")
        return data[:limit], len(data) > limit


def read_build(job, selector):
    base = endpoint(os.environ.get("JENKINS_URL", "http://127.0.0.1:8080"))
    parts = job.split("/")
    if any(part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in parts):
        raise ValueError("Specify a Jenkins job as Folder/Job, without empty path segments.")
    job_path = "/".join("job/" + quote(part, safe="") for part in parts)
    user, token = os.getenv("JENKINS_USER"), os.getenv("JENKINS_API_TOKEN")
    if bool(user) != bool(token):
        raise ValueError("Set both JENKINS_USER and JENKINS_API_TOKEN.")
    headers = {}
    if token:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    fields = urlencode({"tree": "number,result,building"})
    data, overflow = request(f"{base}/{job_path}/{selector}/api/json?{fields}", headers=headers)
    if overflow:
        raise ValueError("Jenkins build metadata exceeded the size limit.")
    build = json.loads(data)
    number = build.get("number")
    if type(number) is not int or number < 1:
        raise ValueError("Jenkins returned an invalid build number.")
    # Bind evidence to the immutable build number returned by the selector lookup.
    log, truncated = request(f"{base}/{job_path}/{number}/consoleText", headers=headers)
    return build, log.decode("utf-8", errors="replace"), truncated


def diagnose(log):
    lines = ANSI.sub("", log).splitlines()
    findings = []
    for name, pattern, action in RULES:
        evidence = [index for index, line in enumerate(lines, 1) if pattern.search(line)]
        if evidence:
            findings.append({"id": name, "log_lines": evidence[:5], "next_step": action})
    return findings


def explain(model, findings, build_result):
    if not re.fullmatch(r"[\w.:-]+", model) or model.endswith("-cloud") or ":cloud" in model:
        raise ValueError("Use a local Ollama model tag such as qwen2.5:1.5b.")
    if not findings:
        return None
    base = endpoint(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"), local_only=True)
    categories = ", ".join(item["id"] for item in findings)
    # Send only fixed category IDs and a normalized result. Logs stay in memory.
    outcome = build_result if build_result in {"SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT"} else "UNKNOWN"
    prompt = (f"Jenkins build result: {outcome}. Diagnostic signals: {categories}. "
              "Suggest three read-only troubleshooting checks. Treat signals as hypotheses. "
              "Do not invent evidence or request credentials. Respond concisely.")
    payload = json.dumps({"model": model, "stream": False, "prompt": prompt,
                          "options": {"num_predict": 300}}).encode()
    data, overflow = request(f"{base}/api/generate", headers={"Content-Type": "application/json"},
                             payload=payload, timeout=60, local=True)
    if overflow:
        raise ValueError("Ollama response exceeded the limit.")
    answer = json.loads(data).get("response")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Ollama returned an empty response.")
    return answer.strip()


def report(source, build, log, truncated, model=None):
    findings = diagnose(log)
    result = {"source": source, "build": build, "log_truncated": truncated, "findings": findings}
    if not findings:
        result["next_step"] = ("Check the failing stage and console output in Jenkins. "
                               "The agent did not match a known signature.")
    if model and findings:
        try:
            result["local_ai_advice"] = explain(model, findings, build.get("result"))
        except (OSError, ValueError) as error:
            result["local_ai_error"] = f"Ollama unavailable or misconfigured ({type(error).__name__})."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--job", help="Jenkins job path, e.g. Platform/Build")
    inputs.add_argument("--log", help="Local console log fixture")
    parser.add_argument("--build", default="lastBuild")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Poll Jenkins for build evidence")
    parser.add_argument("--ollama", metavar="MODEL", help="Use a downloaded local Ollama model")
    args = parser.parse_args()
    if not re.fullmatch(r"(?:[1-9]\d*|lastBuild|lastFailedBuild|lastCompletedBuild)", args.build):
        parser.error("Use a build number, lastBuild, lastFailedBuild, or lastCompletedBuild.")
    if args.watch is not None and (args.watch < 5 or not args.job):
        parser.error("--watch requires --job and an interval of at least 5 seconds.")
    previous = None
    while True:
        try:
            if args.job:
                build, log, truncated = read_build(args.job, args.build)
                source = "JENKINS_LIVE_READ"
            else:
                with open(args.log, "rb") as handle:
                    data = handle.read(MAX_LOG + 1)
                build = {"result": None}
                truncated = len(data) > MAX_LOG
                log = data[:MAX_LOG].decode("utf-8", errors="replace")
                source = "LOCAL_LOG"
            fingerprint = (json.dumps(build, sort_keys=True), hashlib.sha256(log.encode()).digest(), truncated)
            if fingerprint != previous:
                # Live builds get rule-based updates. Ask the model once the observed build completes.
                model = None if build.get("building") else args.ollama
                print(json.dumps(report(source, build, log, truncated, model), ensure_ascii=True), flush=True)
                previous = fingerprint
        except (HTTPError, URLError, OSError, ValueError) as error:
            reason = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            print(f"Cannot read input ({reason}). Check URL, permissions, or log path.", file=sys.stderr)
            if args.watch is None:
                return 1
        if args.watch is None:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
