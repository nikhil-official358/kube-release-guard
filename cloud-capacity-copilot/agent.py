"""Read-only Kubernetes capacity triage with optional local Ollama advice."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

UNITS = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
for power, suffix in enumerate(("k", "M", "G", "T", "P", "E"), 1):
    UNITS[suffix] = 1000 ** power
    UNITS[suffix.upper() + "i"] = 1024 ** power


def quantity(value):
    match = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)((?:[eE][+-]?\d+)|[a-zA-Z]*)", value)
    if not match:
        raise ValueError(f"Invalid Kubernetes quantity: {value}")
    number, suffix = match.groups()
    return Decimal(number + suffix) if re.fullmatch(r"[eE][+-]?\d+", suffix) else Decimal(number) * UNITS[suffix]


def kubectl(context, *args):
    result = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=10s", *args],
        capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(result.stdout)


def inspect(pods, metrics, now, max_age=120):
    samples = {(m["metadata"]["name"]): m for m in metrics["items"]}
    findings = []
    missing = 0
    evaluated = 0
    for pod in pods["items"]:
        if pod.get("status", {}).get("phase") != "Running":
            continue
        name = pod["metadata"]["name"]
        sample = samples.get(name)
        usage = {}
        if sample:
            timestamp = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
            created = pod["metadata"].get("creationTimestamp")
            age = (now - timestamp).total_seconds()
            if 0 <= age <= max_age and (not created or timestamp >= datetime.fromisoformat(created.replace("Z", "+00:00"))):
                usage = {c["name"]: c["usage"] for c in sample["containers"]}
        for container in pod["spec"]["containers"]:
            target = f'{name}/{container["name"]}'
            observed = usage.get(container["name"])
            if observed is None:
                missing += 1
            else:
                evaluated += 1
            for resource in ("cpu", "memory"):
                requests = container.get("resources", {}).get("requests", {})
                requested = quantity(requests.get(resource, "0"))
                record = {"target": target, "resource": resource, "request": requests.get(resource)}
                if requested == 0:
                    findings.append(dict(record, signal="missing_request"))
                elif observed and resource in observed:
                    ratio = quantity(observed[resource]) / requested
                    if ratio <= Decimal("0.25") or ratio >= Decimal("0.90"):
                        signal = "low_usage_sample" if ratio <= Decimal("0.25") else "near_or_above_request"
                        findings.append(dict(record, signal=signal, usage=observed[resource],
                                             usage_pct_of_request=round(float(ratio * 100), 1),
                                             metrics_timestamp=sample["timestamp"]))
    return {"findings": findings, "containers_with_metrics": evaluated,
            "containers_without_fresh_metrics": missing}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_advice(model, findings):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+(?::[a-zA-Z0-9_.-]+)?", model) or "cloud" in model.lower():
        raise ValueError("Choose a downloaded local model.")
    counts = {key: sum(f["signal"] == key for f in findings) for key in
              ("missing_request", "low_usage_sample", "near_or_above_request")}
    body = json.dumps({"model": model, "stream": False, "options": {"num_predict": 200},
                       "prompt": "Give three concise verification steps for these Kubernetes request signals: "
                                 + json.dumps(counts)
                                 + ". A single sample cannot justify resizing. Do not estimate savings."}).encode()
    req = Request("http://127.0.0.1:11434/api/generate", body, {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=45) as response:
        data = json.loads(response.read(65536))
    return data["response"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true")
    source.add_argument("--context")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--ollama", metavar="MODEL")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", args.namespace):
        parser.error("Specify a valid Kubernetes namespace.")
    if args.context and (args.context.startswith("-") or not args.context.strip()):
        parser.error("Supply a named kubeconfig context.")
    if args.watch and args.watch < 10:
        parser.error("Watch interval must be at least 10 seconds.")
    previous = None
    advice = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            metrics_status = "available"
            if args.demo:
                fixture = json.loads(Path(__file__).with_name("demo.json").read_text(encoding="utf-8"))
                pods, metrics = fixture["pods"], fixture["metrics"]
                now = datetime.fromisoformat(fixture["observed_at"])
            else:
                pods = kubectl(args.context, "get", "pods", "-n", args.namespace, "-o", "json")
                try:
                    metrics = kubectl(args.context, "get", "--raw",
                                      f"/apis/metrics.k8s.io/v1beta1/namespaces/{args.namespace}/pods")
                except (subprocess.SubprocessError, ValueError):
                    metrics = {"items": []}
                    metrics_status = "unavailable"
                now = datetime.now(timezone.utc)
            report = inspect(pods, metrics, now)
            report.update(source="DEMO_FIXTURE" if args.demo else "KUBERNETES_LIVE_READ",
                          observed_at=now.isoformat(), metrics_status=metrics_status)
            signals = sorted((f["target"], f["resource"], f["signal"]) for f in report["findings"])
            if args.ollama and signals and signals != previous:
                try:
                    advice = {"local_ai_advice": local_advice(args.ollama, report["findings"])}
                except (OSError, ValueError, KeyError):
                    advice = {"local_ai_status": "unavailable"}
            if signals and advice:
                report.update(advice)
            previous = signals if advice and "local_ai_status" not in advice else None
            print(json.dumps(report), flush=True)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            print(f"Could not read workload data: {detail}", file=sys.stderr)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
