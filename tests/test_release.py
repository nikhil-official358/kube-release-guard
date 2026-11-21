import copy
import json
from pathlib import Path
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import yaml

from app.server import Handler
from gate import audit

ROOT = Path(__file__).resolve().parents[1]

class GateTests(unittest.TestCase):
    def setUp(self):
        self.deployment = yaml.safe_load((ROOT / "k8s/base/deployment.yaml").read_text())
        self.pod = self.deployment["spec"]["template"]["spec"]
        self.container = self.pod["containers"][0]

    def test_base_passes(self):
        self.assertTrue(audit([self.deployment])["passed"])

    def test_missing_deployment_fails(self):
        self.assertEqual(audit([None])["findings"][0]["rule"], "deployment-required")

    def test_insecure_container_fails(self):
        self.container["securityContext"]["capabilities"]["add"] = ["NET_ADMIN"]
        self.container["securityContext"]["runAsUser"] = 0
        self.container.pop("readinessProbe")
        self.assertEqual(
            {finding["rule"] for finding in audit([self.deployment])["findings"]},
            {"drop-all-capabilities", "non-root-container-override", "readiness-and-liveness"},
        )

    def test_init_container_requires_security_without_probes(self):
        init = copy.deepcopy(self.container)
        init.pop("readinessProbe")
        init.pop("livenessProbe")
        self.pod["initContainers"] = [init]
        self.assertTrue(audit([self.deployment])["passed"])
        init.pop("resources")
        self.assertEqual(audit([self.deployment])["findings"][0]["rule"], "cpu-and-memory-budget")

    def test_digest_requirement(self):
        self.assertFalse(audit([self.deployment], require_digest=True)["passed"])
        self.container["image"] = "example.invalid/demo@sha256:" + "a" * 64
        self.assertTrue(audit([self.deployment], require_digest=True)["passed"])

class ServiceTests(unittest.TestCase):
    def test_http_contract(self):
        Handler.requests = 0
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for path in ("/healthz", "/readyz"):
                    with urlopen(base + path, timeout=3) as response:
                        self.assertEqual(json.load(response), {"status": "ok"})
                with urlopen(base, timeout=3) as response:
                    self.assertEqual(json.load(response)["service"], "release-demo")
                with urlopen(base + "/metrics", timeout=3) as response:
                    self.assertIn("release_demo_requests_total 1", response.read().decode())
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + "/missing", timeout=3)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            finally:
                server.shutdown()
                thread.join()
