import asyncio
import base64
import json
import unittest

from bridge.core import BridgeError, Settings, handle
from bridge.execution import execute_inline, execute_subprocess
from test_bridge import FakeGitHub, KEY, SHA, request

EDIT = '''from pathlib import Path
def run(root, data):
    path = Path(root) / "docs/筆記.md"
    path.write_text("# Updated\\n", encoding="utf-8")
    (Path(root) / "docs/new.md").write_text("new", encoding="utf-8")
    return {"saved": 2}
'''.encode("utf-8")


def settings():
    return Settings(KEY, {"owner/private": {"ref": "main", "program_prefixes": ["program"],
        "data_prefixes": ["docs"], "write_refs": ["main"], "write_prefixes": ["docs"]}}, "private-credential")


class WritableGitHub(FakeGitHub):
    def __init__(self, source=EDIT, move_before_write=False, ref_race=False):
        super().__init__(program=source)
        self.writes = []
        self.branch_reads = 0
        self.move_before_write, self.ref_race = move_before_write, ref_race

    async def fetch(self, url, headers):
        if url.endswith("/git/ref/heads/main"):
            self.branch_reads += 1
            if self.move_before_write and self.branch_reads > 1:
                return {"object": {"sha": "1" * 40}}
        return await super().fetch(url, headers)

    async def send(self, method, url, headers, body):
        assert url.startswith("https://api.github.com/repos/owner/private/git/")
        self.writes.append((method, url, body))
        if url.endswith("/trees"):
            assert body["base_tree"] == "b" * 40
            return {"sha": "e" * 40}
        if url.endswith("/commits"):
            assert body["parents"] == [SHA]
            assert body["tree"] == "e" * 40
            return {"sha": "f" * 40}
        assert method == "PATCH" and url.endswith("/refs/heads/main")
        assert body == {"sha": "f" * 40, "force": False}
        if self.ref_race:
            raise BridgeError("branch_conflict", 409)
        return {"object": {"sha": "f" * 40}}


class WriteConformance(unittest.TestCase):
    def call(self, fake=None, execute=execute_subprocess, **overrides):
        fake = fake or WritableGitHub()
        options = {"write": {"message": "Update notes", "expected_commit": SHA}, **overrides}
        result = asyncio.run(handle(request(**options), "Bearer " + KEY, settings(), fake.fetch, execute, fake.send))
        return result, fake

    def test_both_adapters_commit_all_changes_atomically(self):
        for execute in (execute_inline, execute_subprocess):
            with self.subTest(adapter=execute.__name__):
                (status, body), fake = self.call(execute=execute)
                self.assertEqual(status, 200, body)
                self.assertEqual(body["result"], {"saved": 2})
                self.assertEqual(body["write"], {"commit": "f" * 40, "changed": ["docs/new.md", "docs/筆記.md"]})
                self.assertEqual([item[0] for item in fake.writes], ["POST", "POST", "PATCH"])
                self.assertEqual(len(fake.writes[0][2]["tree"]), 2)

    def test_read_request_discards_changes(self):
        fake = WritableGitHub()
        status, body = asyncio.run(handle(request(), "Bearer " + KEY, settings(), fake.fetch, execute_subprocess, fake.send))
        self.assertEqual(status, 200, body)
        self.assertNotIn("write", body)
        self.assertFalse(fake.writes)

    def test_explicit_delete(self):
        source = 'from pathlib import Path\ndef run(root, data): (Path(root)/"docs/筆記.md").unlink(); return True'.encode()
        (status, body), fake = self.call(fake=WritableGitHub(source))
        self.assertEqual(status, 200, body)
        self.assertEqual(fake.writes[0][2]["tree"], [{"path": "docs/筆記.md", "mode": "100644", "type": "blob", "sha": None}])

    def test_stale_expected_commit_never_executes_or_writes(self):
        (status, body), fake = self.call(write={"message": "stale", "expected_commit": "0" * 40})
        self.assertEqual((status, body["error"]["code"]), (409, "branch_conflict"))
        self.assertEqual(fake.calls, ["/git/ref/heads/main"])
        self.assertFalse(fake.writes)

    def test_concurrent_change_before_commit(self):
        (status, body), fake = self.call(fake=WritableGitHub(move_before_write=True))
        self.assertEqual((status, body["error"]["code"]), (409, "branch_conflict"))
        self.assertFalse(fake.writes)

    def test_race_during_final_ref_update(self):
        (status, body), fake = self.call(fake=WritableGitHub(ref_race=True))
        self.assertEqual((status, body["error"]["code"]), (409, "branch_conflict"))
        self.assertFalse(fake.writes[-1][2]["force"])

    def test_unread_existing_file_not_overwritten(self):
        fake = WritableGitHub()
        fake.responses["/git/trees/" + "d" * 40]["tree"].append({
            "path": "new.md", "type": "blob", "mode": "100644", "size": 1, "sha": "2" * 40})
        (status, body), fake = self.call(fake=fake)
        self.assertEqual((status, body["error"]["code"]), (409, "unread_file_conflict"))
        self.assertFalse(fake.writes)

    def test_write_scope_checked_before_any_mutation(self):
        source = b'from pathlib import Path\ndef run(root, data): (Path(root)/"other.md").write_text("x"); return True'
        (status, body), fake = self.call(fake=WritableGitHub(source))
        self.assertEqual((status, body["error"]["code"]), (403, "write_path_not_allowed"))
        self.assertFalse(fake.writes)

    def test_failed_program_does_not_commit_partial_changes(self):
        source = b'from pathlib import Path\ndef run(root, data):\n (Path(root)/"docs/new.md").write_text("x")\n raise ValueError("failure")'
        for execute in (execute_subprocess, execute_inline):
            (status, body), fake = self.call(fake=WritableGitHub(source), execute=execute)
            self.assertNotEqual(status, 200)
            self.assertFalse(fake.writes)

    def test_symlink_output_rejected(self):
        source = b'from pathlib import Path\ndef run(root, data): (Path(root)/"docs/link").symlink_to("/etc/passwd"); return True'
        (status, body), fake = self.call(fake=WritableGitHub(source))
        self.assertEqual((status, body["error"]["code"]), (422, "unsupported_repository_entry"))
        self.assertFalse(fake.writes)

    def test_no_changes_does_not_create_commit(self):
        (status, body), fake = self.call(fake=WritableGitHub(b'def run(root, data): return True'))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["write"], {"commit": SHA, "changed": []})
        self.assertFalse(fake.writes)

    def test_invalid_write_options_and_disabled_write_branch(self):
        for value in ({}, True, {"message": "x", "expected_commit": "main"}):
            (status, body), fake = self.call(write=value)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_write_request"))
            self.assertFalse(fake.calls)
        conf = settings()
        conf.repositories["owner/private"]["write_refs"] = []
        fake = WritableGitHub()
        status, body = asyncio.run(handle(request(write={"message": "x", "expected_commit": SHA}),
            "Bearer " + KEY, conf, fake.fetch, execute_subprocess, fake.send))
        self.assertEqual((status, body["error"]["code"]), (403, "write_not_allowed"))
        self.assertFalse(fake.calls)


if __name__ == "__main__":
    unittest.main()
