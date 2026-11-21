"""Check release requirements on rendered Kubernetes Deployments."""
import argparse
import json
import re
import sys

import yaml

DIGEST = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")

def audit(documents, require_digest=False):
    findings = []
    deployments = 0
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        deployments += 1
        metadata = doc["metadata"]
        deployment = f'{metadata.get("namespace", "default")}/{metadata["name"]}'
        template = doc["spec"]["template"]["spec"]
        pod_security = template.get("securityContext", {})
        if pod_security.get("runAsNonRoot") is not True or not pod_security.get("runAsUser", 0) > 0:
            findings.append({"target": deployment, "rule": "pod-non-root"})
        if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            findings.append({"target": deployment, "rule": "runtime-seccomp"})
        if template.get("automountServiceAccountToken") is not False:
            findings.append({"target": deployment, "rule": "disable-token-mount"})
        for group in ("containers", "initContainers"):
            for container in template.get(group, []):
                target = f'{deployment}/{container["name"]}'
                security = container.get("securityContext", {})
                resources = container.get("resources", {})
                checks = {
                    "no-privilege-escalation": security.get("allowPrivilegeEscalation") is False,
                    "no-privileged-container": security.get("privileged", False) is False,
                    "drop-all-capabilities": (
                        "ALL" in security.get("capabilities", {}).get("drop", [])
                        and not security.get("capabilities", {}).get("add")
                    ),
                    "read-only-root": security.get("readOnlyRootFilesystem") is True,
                    "cpu-and-memory-budget": all(
                        resources.get(bound, {}).get(unit)
                        for bound in ("requests", "limits") for unit in ("cpu", "memory")
                    ),
                    "non-root-container-override": (
                        security.get("runAsNonRoot", True) is True
                        and security.get("runAsUser", pod_security.get("runAsUser", 0)) > 0
                    ),
                }
                if group == "containers":
                    checks["readiness-and-liveness"] = bool(
                        container.get("readinessProbe") and container.get("livenessProbe")
                    )
                if require_digest:
                    checks["pinned-image-digest"] = bool(DIGEST.fullmatch(container.get("image", "")))
                findings.extend({"target": target, "rule": rule} for rule, passed in checks.items() if not passed)
    if deployments == 0:
        findings.append({"target": "manifest", "rule": "deployment-required"})
    return {"passed": not findings, "deployments": deployments, "findings": findings}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-digest", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(yaml.safe_load_all(sys.stdin), args.require_digest)
    except (yaml.YAMLError, TypeError, KeyError, AttributeError) as error:
        print(json.dumps({"passed": False, "error": f"Invalid manifest: {error}"}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
