"""Authoring contract and create/execute/edit loop through the real runner."""
from pathlib import Path
import unittest

from bridge.core import BridgeError, Settings
from bridge.execution import execute_subprocess
from test_bridge import KEY


class AuthoringTests(unittest.TestCase):
    def test_runtime_policy_rejects_application_helper_configuration(self):
        policy = {"ref": "main", "program_prefixes": ["helper", "workspace/programs"],
                  "data_prefixes": ["workspace/data"]}
        Settings(KEY, {"owner/private": policy})
        for field in ("authoring", "repo_files", "custom_workflow"):
            with self.subTest(field=field), self.assertRaises(BridgeError):
                Settings(KEY, {"owner/private": {**policy, field: {"program": "helper/files.py"}}})

    def test_create_read_execute_and_revise_ordinary_python(self):
        helper = "helper/files.py"
        program = "workspace/programs/calculate.py"
        baseline = {helper: Path("examples/workspace_files/main.py").read_bytes()}
        first = 'def run(root, request):\n    return {"value": sum(request["numbers"])}\n'
        saved = execute_subprocess(baseline, helper, {"changes": {program: first}})
        self.assertEqual(saved.changes, {program: first.encode()})
        baseline.update(saved.changes)
        loaded = execute_subprocess(baseline, helper, {"read": [program]})
        self.assertEqual(loaded.result["files"][program], first)
        self.assertEqual(execute_subprocess(baseline, program, {"numbers": [2, 3]}).result, {"value": 5})
        second = 'def run(root, request):\n    return {"value": sum(request["numbers"]) * 2}\n'
        updated = execute_subprocess(baseline, helper, {"changes": {program: second}})
        baseline.update(updated.changes)
        self.assertEqual(execute_subprocess(baseline, program, {"numbers": [2, 3]}).result, {"value": 10})


if __name__ == "__main__":
    unittest.main()
