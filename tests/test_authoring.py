"""Authoring contract and create/execute/edit loop through the real runner."""
from copy import deepcopy
from pathlib import Path
import unittest

from bridge.core import BridgeError, Settings
from bridge.execution import execute_subprocess
from test_bridge import KEY


class AuthoringTests(unittest.TestCase):
    def test_authoring_requires_existing_read_write_and_execution_permissions(self):
        policy = {"ref": "main", "additional_refs": ["workspace"],
                  "program_prefixes": ["helper", "workspace/programs"],
                  "data_prefixes": ["workspace/data"], "write_refs": ["workspace"],
                  "write_prefixes": ["workspace"],
                  "authoring": {"ref": "workspace", "program": "helper/files.py",
                                "program_prefix": "workspace/programs", "data_prefix": "workspace/data"}}
        Settings(KEY, {"owner/private": policy})
        for field, value in [("ref", "main"), ("program", "outside/helper.py"),
                             ("program_prefix", "helper"), ("program_prefix", "workspace/data"),
                             ("data_prefix", "outside/data")]:
            with self.subTest(field=field, value=value):
                invalid = deepcopy(policy)
                invalid["authoring"][field] = value
                with self.assertRaises(BridgeError):
                    Settings(KEY, {"owner/private": invalid})

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
