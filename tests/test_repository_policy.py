"""Whole-repository reads never widen execution or branch-specific writes."""
import asyncio
from copy import deepcopy
import unittest

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.core import BridgeError, Execution, Settings, handle, parse_request
from bridge.execution import execute_subprocess
from bridge.mcp_server import create_server
from test_bridge import KEY, SHA, FakeGitHub, request
from test_mcp import AUTH, rpc
from test_writes import WritableGitHub

POLICY = {
    "ref": "main", "read_all": True,
    "program_prefixes": ["program", "workspace/programs"],
    "additional_refs": ["workspace"], "write_refs": ["main", "workspace"],
    "write_prefixes_by_ref": {"main": ["memory/story"], "workspace": ["workspace"]},
    "repo_files": {"ref": "main", "program": "program/main.py"},
    "authoring": {"ref": "workspace", "program": "program/main.py",
                  "program_prefix": "workspace/programs", "data_prefix": "workspace/data"},
}
WRITER = b'''from pathlib import Path
def run(root, data):
    for name, text in data["changes"].items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return {"ok": True}
'''


class RepositoryPolicyTests(unittest.TestCase):
    def full_write_policy(self):
        policy = deepcopy(POLICY)
        del policy["write_prefixes_by_ref"]["main"]
        policy["write_all_refs"] = ["main"]
        return policy

    def config(self, policy=None):
        return Settings(KEY, {"owner/private": deepcopy(POLICY if policy is None else policy)},
                        "private-credential")

    def test_arbitrary_repository_files_allowed_but_not_arbitrary_execution(self):
        paths = ["AGENTS.md", "memory/skill/example.py", "memory/story/STORIES.md",
                 "memory/fable/example.md", "assets/new-category/file.md", "launchers/run.sh"]
        cfg = self.config()
        parsed = parse_request(request(files=paths), "Bearer " + KEY, cfg)
        self.assertEqual(parsed["files"], paths)
        with self.assertRaisesRegex(BridgeError, "program_not_allowed"):
            parse_request(request(program="memory/skill/example.py"), "Bearer " + KEY, cfg)

    def test_whole_read_retains_path_and_repository_boundaries_before_network(self):
        for path in ["../escape", "/etc/passwd", "assets/../../escape", "C:/escape",
                     "assets\\..\\escape", ".git/config", "file:///etc/passwd"]:
            fake = FakeGitHub()
            status, body = asyncio.run(handle(request(files=[path]), "Bearer " + KEY,
                self.config(), fake.fetch, execute_subprocess))
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_path"), path)
            self.assertEqual(fake.calls, [])
        with self.assertRaisesRegex(BridgeError, "repository_not_allowed"):
            parse_request(request(repository="another/repo"), "Bearer " + KEY, self.config())

    def test_whole_read_still_rejects_symlinks_and_submodules(self):
        for mode in ["120000", "160000"]:
            fake = FakeGitHub(mode=mode)
            status, body = asyncio.run(handle(request(), "Bearer " + KEY, self.config(),
                                             fake.fetch, execute_subprocess))
            self.assertEqual((status, body["error"]["code"]), (422, "unsupported_repository_entry"))

    def write(self, changes, ref="main", policy=None):
        fake = WritableGitHub(WRITER)
        if ref != "main":
            fake.responses["/git/ref/heads/" + ref] = {"object": {"sha": SHA}}
        status, body = asyncio.run(handle(request(ref=ref, files=[], input={"changes": changes},
            write={"message": "Test transaction", "expected_commit": SHA}), "Bearer " + KEY,
            self.config(policy), fake.fetch, execute_subprocess, fake.send))
        return status, body, fake

    def test_main_story_batch_commits_once(self):
        changes = {"memory/story/projects/Test/EPISODES/one.md": "episode",
                   "memory/story/projects/Test/STORY.md": "story",
                   "memory/story/projects/STORIES.md": "index"}
        status, body, fake = self.write(changes)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["write"]["changed"], sorted(changes))
        self.assertEqual([w[0] for w in fake.writes], ["POST", "POST", "PATCH"])
        self.assertEqual(len(fake.writes[0][2]["tree"]), 3)

    def test_mixed_batch_never_partially_writes_outside_main_story(self):
        for denied in ["AGENTS.md", "memory/skill/SKILL.md", "workspace/data/file.md",
                       "memory/story-other/file.md"]:
            status, body, fake = self.write({"memory/story/test.md": "allowed", denied: "denied"})
            self.assertEqual((status, body["error"]["code"]), (403, "write_path_not_allowed"))
            self.assertEqual(fake.writes, [])

    def test_workspace_does_not_inherit_main_story_grant(self):
        status, body, fake = self.write({"memory/story/test.md": "denied"}, ref="workspace")
        self.assertEqual((status, body["error"]["code"]), (403, "write_path_not_allowed"))
        self.assertEqual(fake.writes, [])
        cfg = self.config()
        parse_request(request(ref="workspace", write={"message": "workspace", "expected_commit": SHA}),
                      "Bearer " + KEY, cfg)

    def test_missing_ref_grant_and_invalid_policy_fail_closed(self):
        policy = deepcopy(POLICY)
        del policy["write_prefixes_by_ref"]["main"]
        status, body, fake = self.write({}, policy=policy)
        self.assertEqual((status, body["error"]["code"]), (403, "write_not_allowed"))
        self.assertEqual(fake.calls, [])
        for delta in [{"read_all": "true"}, {"write_prefixes": ["memory"]},
                      {"write_prefixes_by_ref": {"unknown": ["memory/story"]}},
                      {"repo_files": {"ref": "main", "program": "not-allowed/helper.py"}}]:
            with self.subTest(delta=delta), self.assertRaises(BridgeError):
                self.config({**POLICY, **delta})

    def test_discovery_exposes_read_mode_ref_grants_and_canonical_contract(self):
        server = create_server(self.config(), StaticTokenVerifier(tokens={AUTH: {"client_id": "test", "scopes": []}}))
        app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
        with TestClient(app) as client:
            result = rpc(client, "tools/call", {"name": "list_runtime_targets", "arguments": {}}).json()["result"]
            self.assertEqual(result["structuredContent"]["repositories"]["owner/private"], POLICY)
            self.assertIn("repo_files_usage", result["structuredContent"])
            self.assertIn("authoring_usage", result["structuredContent"])

    def test_whole_write_main_cross_directory_batch_and_workspace_isolation(self):
        policy = self.full_write_policy()
        changes = {path: "UTF-8 驗收" for path in ["AGENTS.md", "memory/skill/SKILL.md",
            "memory/story/test.md", "memory/fable/test.md", "assets/daily/test.md",
            "launchers/test.sh", "future-category/test.txt", "root-file.txt"]}
        status, body, fake = self.write(changes, policy=policy)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["write"]["changed"], sorted(changes))
        self.assertEqual([w[0] for w in fake.writes], ["POST", "POST", "PATCH"])
        self.assertEqual(len(fake.writes[0][2]["tree"]), len(changes))
        status, body, fake = self.write(changes, ref="workspace", policy=policy)
        self.assertEqual((status, body["error"]["code"]), (403, "write_path_not_allowed"))
        self.assertEqual(fake.writes, [])

    def test_whole_write_configuration_requires_explicit_unambiguous_ref_grants(self):
        full = self.full_write_policy()
        for delta in [{"write_all_refs": True}, {"write_all_refs": "main"},
                      {"write_all_refs": [True]}, {"write_all_refs": ["unknown"]},
                      {"write_refs": ["workspace"]}, {"write_prefixes": ["memory"]},
                      {"write_prefixes_by_ref": POLICY["write_prefixes_by_ref"]}]:
            with self.subTest(delta=delta), self.assertRaises(BridgeError):
                self.config({**full, **delta})
        # Removing the all-write grant does not fall back to another branch's grant.
        status, body, fake = self.write({}, policy={**full, "write_all_refs": []})
        self.assertEqual((status, body["error"]["code"]), (403, "write_not_allowed"))
        self.assertEqual(fake.calls, [])

    def test_whole_write_batch_keeps_path_and_git_entry_boundary_before_mutation(self):
        paths = ["../escape", "/tmp/escape", "assets/../../escape", "C:/escape",
                 "assets\\..\\escape", ".git/config", "file:///etc/passwd"]
        cases = [(path, None, "invalid_path") for path in paths]
        cases += [(path, mode, "unsupported_repository_entry")
                  for mode in ["120000", "160000"]
                  for path in ["z-link", "z-link/child.txt"]]
        for path, mode, error in cases:
            fake = WritableGitHub(WRITER)
            if mode:
                fake.responses["/git/trees/" + "b" * 40]["tree"].append({
                    "path": "z-link", "mode": mode, "type": "blob" if mode == "120000" else "commit",
                    "sha": "2" * 40})
            execution = Execution({}, {"assets/valid.txt": b"valid", path: b"invalid"})
            status, body = asyncio.run(handle(request(files=[],
                write={"message": "boundary", "expected_commit": SHA}), "Bearer " + KEY,
                self.config(self.full_write_policy()), fake.fetch,
                lambda *args: execution, fake.send))
            self.assertNotEqual(status, 200, path)
            self.assertEqual(body["error"]["code"], error, path)
            self.assertEqual(fake.writes, [], path)

    def test_whole_write_keeps_commit_and_unread_file_guards(self):
        for expected, files, error in [("0" * 40, [], "branch_conflict"),
                                       (SHA, [], "unread_file_conflict")]:
            fake = WritableGitHub(WRITER)
            status, body = asyncio.run(handle(request(files=files,
                write={"message": "guard", "expected_commit": expected}), "Bearer " + KEY,
                self.config(self.full_write_policy()), fake.fetch,
                lambda *args: Execution({}, {"assets/new.txt": b"new", "docs/筆記.md": b"edit"}),
                fake.send))
            self.assertEqual((status, body["error"]["code"]), (409, error))
            self.assertEqual(fake.writes, [])

    def test_discovery_exposes_whole_write_ref_without_prefix_workaround(self):
        policy = self.full_write_policy()
        server = create_server(self.config(policy), StaticTokenVerifier(tokens={AUTH: {"client_id": "test", "scopes": []}}))
        with TestClient(server.http_app(path="/mcp", stateless_http=True, json_response=True)) as client:
            result = rpc(client, "tools/call", {"name": "list_runtime_targets", "arguments": {}}).json()["result"]["structuredContent"]
            self.assertEqual(result["repositories"]["owner/private"], policy)
            self.assertIn("write_all_refs", result["repo_files_usage"]["boundaries"])
            self.assertIn("authoring_usage", result)


if __name__ == "__main__":
    unittest.main()
