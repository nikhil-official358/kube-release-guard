import json
import unittest
from unittest.mock import patch

import agent


class AgentTests(unittest.TestCase):
    def test_finds_multiple_causes_with_line_numbers(self):
        findings = agent.diagnose("stage\nno space left on device\nexit code 137\n")
        self.assertEqual([(f["id"], f["log_lines"]) for f in findings],
                         [("disk_full", [2]), ("process_killed", [3])])

    def test_jenkins_pins_log_to_number_for_folder_job(self):
        with patch.dict(agent.os.environ, {"JENKINS_URL": "https://jenkins.example", "JENKINS_USER": "reader",
                                           "JENKINS_API_TOKEN": "sample"}, clear=True):
            with patch("agent.request", side_effect=[(b'{"number":17,"result":"FAILURE"}', False),
                                                     (b"no space left on device", False)]) as request:
                build, log, truncated = agent.read_build("Folder/My Build", "lastBuild")
        self.assertEqual(build["number"], 17)
        self.assertIn("/job/Folder/job/My%20Build/17/consoleText", request.call_args.args[0])
        self.assertIn("Authorization", request.call_args.kwargs["headers"])
        self.assertIn("no space", log)
        self.assertFalse(truncated)

    def test_ollama_payload_contains_category_without_jenkins_log(self):
        with patch.dict(agent.os.environ, {}, clear=True):
            with patch("agent.request", return_value=(b'{"response":"Check the agent."}', False)) as request:
                result = agent.report("LOCAL_LOG", {}, "no space left on device secret=raw-value",
                                      False, "qwen2.5:1.5b")
        self.assertEqual(result["local_ai_advice"], "Check the agent.")
        payload = json.loads(request.call_args.kwargs["payload"])
        self.assertIn("disk_full", payload["prompt"])
        self.assertNotIn("raw-value", str(result) + payload["prompt"])

    def test_ollama_failure_preserves_local_diagnosis(self):
        with patch("agent.explain", side_effect=OSError):
            result = agent.report("LOCAL_LOG", {}, "no space left on device", False, "example")
        self.assertEqual(result["findings"][0]["id"], "disk_full")
        self.assertIn("local_ai_error", result)

    def test_remote_ollama_rejected(self):
        with patch.dict(agent.os.environ, {"OLLAMA_URL": "https://example.invalid"}, clear=True):
            with self.assertRaises(ValueError):
                agent.explain("qwen2.5:1.5b", [{"id": "disk_full"}], "FAILURE")

    def test_remote_plaintext_jenkins_rejected(self):
        with self.assertRaises(ValueError):
            agent.endpoint("http://jenkins.example")
