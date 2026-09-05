import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from bridge.core import BridgeError, MAX_REQUEST, Settings, handle
from bridge.execution import execute_inline, execute_subprocess

KEY = "test-key-" + "x" * 32
SHA = "a" * 40
PROBE = Path("examples/markdown_titles/main.py").read_bytes()


def settings():
    return Settings(KEY, {"owner/private": {"ref": "main", "program_prefixes": ["program"],
                                             "data_prefixes": ["docs"]}}, "private-credential")


def request(**updates):
    value = {"repository": "owner/private", "ref": "main", "program": "program/main.py",
             "files": ["docs/筆記.md"], "input": {"files": ["docs/筆記.md"]}}
    value.update(updates)
    return json.dumps(value, ensure_ascii=False).encode()


class FakeGitHub:
    def __init__(self, program=PROBE, mode="100644", corrupt=False):
        self.calls = []
        self.responses = {
            "/git/ref/heads/main": {"object": {"sha": SHA}},
            "/git/commits/" + SHA: {"tree": {"sha": "b" * 40}},
            "/git/trees/" + "b" * 40: {"tree": [
                {"path": "program", "type": "tree", "mode": "040000", "sha": "c" * 40},
                {"path": "docs", "type": "tree", "mode": "040000", "sha": "d" * 40}]}}
        for tree, name, content, filemode in (("c", "main.py", program, mode),
                                            ("d", "筆記.md", "# 你好\n正文".encode(), "100644")):
            sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            self.responses["/git/trees/" + tree * 40] = {"tree": [
                {"path": name, "type": "blob", "mode": filemode, "sha": sha, "size": len(content)}]}
            self.responses["/git/blobs/" + sha] = {"encoding": "base64", "content":
                base64.b64encode(b"x" * len(content) if corrupt else content).decode()}

    async def fetch(self, url, headers):
        assert url.startswith("https://api.github.com/repos/owner/private/")
        assert headers["Authorization"] == "Bearer private-credential"
        path = url.removeprefix("https://api.github.com/repos/owner/private")
        self.calls.append(path)
        return self.responses[path]


class Conformance(unittest.TestCase):
    def call(self, raw=None, auth=None, fake=None, execute=execute_subprocess):
        fake = fake or FakeGitHub()
        result = asyncio.run(handle(raw or request(), auth if auth is not None else "Bearer " + KEY,
                                    settings(), fake.fetch, execute))
        return result, fake

    def test_same_probe_and_result_in_both_execution_adapters(self):
        for execute in (execute_subprocess, execute_inline):
            with self.subTest(adapter=execute.__name__):
                (status, body), fake = self.call(execute=execute)
                self.assertEqual(status, 200, body)
                self.assertEqual(body["result"], {"files": [{"path": "docs/筆記.md", "title": "你好", "line_count": 2}], "count": 1})
                self.assertEqual(body["source"]["commit"], SHA)
                self.assertEqual(body["source"]["sha256"], hashlib.sha256(PROBE).hexdigest())
                self.assertEqual(fake.calls.count("/git/ref/heads/main"), 1)

    def test_authentication_before_network(self):
        for auth in ("", "Bearer wrong", "Bearer 秘密"):
            (status, _), fake = self.call(auth=auth)
            self.assertEqual(status, 401)
            self.assertEqual(fake.calls, [])

    def test_policy_and_path_rejections_before_network(self):
        cases = [({"repository": "other/repo"}, 403), ({"ref": "attacker-branch"}, 403),
                 ({"program": "docs/malware.py"}, 403), ({"program": "program/../../bad.py"}, 400),
                 ({"files": ["/etc/passwd"]}, 400), ({"files": ["docs/../secret"]}, 400),
                 ({"files": ["docs\\secret"]}, 400), ({"files": ["docs/.git/config"]}, 400),
                 ({"files": ["secrets/token"]}, 403), ({"input": []}, 400),
                 ({"files": ["docs/x", "docs/x"]}, 400), ({"unexpected": True}, 400)]
        for update, expected in cases:
            with self.subTest(update=update):
                (status, _), fake = self.call(raw=request(**update))
                self.assertEqual(status, expected)
                self.assertFalse(fake.calls)

    def test_invalid_and_oversized_json(self):
        for raw, expected in ((b"{", 400), (b"[]", 400), (b"x" * (MAX_REQUEST + 1), 413)):
            (status, _), fake = self.call(raw=raw)
            self.assertEqual(status, expected)
            self.assertFalse(fake.calls)

    def test_symlink_and_submodule_never_execute(self):
        for mode in ("120000", "160000"):
            (status, body), fake = self.call(fake=FakeGitHub(mode=mode))
            self.assertEqual(status, 422)
            self.assertEqual(body["error"]["code"], "unsupported_repository_entry")
            self.assertFalse(any("/git/blobs/" in path for path in fake.calls))

    def test_corrupted_blob_rejected(self):
        (status, body), _ = self.call(fake=FakeGitHub(corrupt=True))
        self.assertEqual((status, body["error"]["code"]), (502, "invalid_upstream_response"))

    def test_canonical_update_without_bridge_change(self):
        first, _ = self.call(fake=FakeGitHub(program=b'def run(root, data): return {"version": 1}'))
        second, _ = self.call(fake=FakeGitHub(program=b'def run(root, data): return {"version": 2}'))
        self.assertEqual(first[1]["result"], {"version": 1})
        self.assertEqual(second[1]["result"], {"version": 2})
        self.assertNotEqual(first[1]["source"]["sha256"], second[1]["source"]["sha256"])

    def test_private_exception_and_logs_not_returned(self):
        source = b'def run(root, data):\n print("private log")\n raise ValueError("private credential")'
        for execute in (execute_subprocess, execute_inline):
            (status, body), _ = self.call(fake=FakeGitHub(program=source), execute=execute)
            self.assertNotEqual(status, 200)
            self.assertNotIn("private", json.dumps(body))

    def test_child_has_no_deployment_secrets(self):
        source = b'import os\ndef run(root, data): return os.environ.get("BRIDGE_GITHUB_TOKEN")'
        with patch.dict(os.environ, {"BRIDGE_GITHUB_TOKEN": "do-not-pass"}):
            (status, body), _ = self.call(fake=FakeGitHub(program=source))
        self.assertEqual(status, 200)
        self.assertIsNone(body["result"])

    def test_child_timeout(self):
        with self.assertRaises(BridgeError) as error:
            execute_subprocess({"main.py": b'def run(root, data):\n while True: pass'}, "main.py", {}, timeout=0.2)
        self.assertEqual(error.exception.code, "execution_timeout")

    def test_oversized_result(self):
        source = b'def run(root, data): return "x" * 600000'
        for execute in (execute_subprocess, execute_inline):
            (status, body), _ = self.call(fake=FakeGitHub(program=source), execute=execute)
            self.assertNotEqual(status, 200)
            self.assertEqual(body["error"]["code"], "result_too_large")

    def test_snapshot_cleaned_between_requests(self):
        source = b'from pathlib import Path\ndef run(root, data): return str(Path(root))'
        paths = []
        for execute in (execute_subprocess, execute_inline):
            for _ in range(2):
                (status, body), _ = self.call(fake=FakeGitHub(program=source), execute=execute)
                self.assertEqual(status, 200)
                paths.append(body["result"])
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_missing_configuration_fails_closed(self):
        with self.assertRaises(BridgeError) as error:
            Settings.from_env({})
        self.assertEqual(error.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
