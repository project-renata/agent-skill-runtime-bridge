"""Filesystem materialization and immutable data/code revisions use one runtime."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.core import BridgeError, Settings, handle
from bridge.execution import execute_inline, execute_subprocess
from bridge.mcp_server import create_server
from test_bridge import KEY, request
from test_mcp import AUTH, rpc

HEAD, OLD = "a" * 40, "1" * 40
PROGRAM = "program/scan.py"
SCAN = b'''from pathlib import Path
def run(root, input):
    root = Path(root)
    for name, text in input.get("changes", {}).items():
        path = root / name
        if text is None: path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return {"paths": sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()),
            "text": (root / input["read"]).read_text(encoding="utf-8") if "read" in input else None}
'''
POLICY = {"ref": "main", "read_all": True, "program_prefixes": ["program"],
          "write_refs": ["main"], "write_all_refs": ["main"]}


def config(policy=None):
    return Settings(KEY, {"owner/private": deepcopy(POLICY if policy is None else policy)}, "private-credential")


class SnapshotGitHub:
    def __init__(self, current=None, old=None, extras=()):
        current = current or {PROGRAM: SCAN, "AGENTS.md": b"os", "docs/a.md": b"current",
            "docs/nested/b.md": "中文".encode(), "docs/later.md": b"new", "fables/f.md": b"fable"}
        old = old or {**current, "docs/a.md": b"historical"}
        old = {p: b for p, b in old.items() if p != "docs/later.md"}
        self.responses, self.calls, self.writes = {}, [], []
        self.root_trees = {}
        for commit, files in [(HEAD, current), (OLD, old)]:
            nodes = {}
            for path, content in files.items():
                node = nodes
                parts = path.split("/")
                for part in parts[:-1]: node = node.setdefault(part, {})
                node[parts[-1]] = content
            def tree(nodes):
                entries, recursive = [], []
                for name, value in sorted(nodes.items()):
                    if isinstance(value, dict):
                        sha, nested = tree(value)
                        item = {"path": name, "mode": "040000", "type": "tree", "sha": sha}
                        recursive.extend({**x, "path": name + "/" + x["path"]} for x in nested)
                    else:
                        sha = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
                        self.responses["/git/blobs/" + sha] = {"encoding": "base64", "content": base64.b64encode(value).decode()}
                        item = {"path": name, "mode": "100644", "type": "blob", "sha": sha, "size": len(value)}
                    entries.append(item); recursive.append(item)
                sha = hashlib.sha1(json.dumps(entries).encode()).hexdigest()
                self.responses["/git/trees/" + sha] = {"tree": entries}
                self.responses["/git/trees/" + sha + "?recursive=1"] = {"tree": recursive, "truncated": False}
                return sha, recursive
            root, _ = tree(nodes)
            self.root_trees[commit] = root
            self.responses["/git/commits/" + commit] = {"sha": commit, "tree": {"sha": root}}
        self.responses["/git/ref/heads/main"] = {"object": {"sha": HEAD}}
        self.docs_tree = next(e["sha"] for e in self.responses["/git/trees/" + self.root_trees[HEAD]]["tree"] if e["path"] == "docs")
        self.responses["/git/trees/" + self.docs_tree]["tree"].extend(extras)
        self.responses["/git/trees/" + self.docs_tree + "?recursive=1"]["tree"].extend(extras)

    async def fetch(self, url, headers):
        prefix = "https://api.github.com/repos/owner/private"
        assert url.startswith(prefix)
        assert headers["Authorization"] == "Bearer private-credential"
        path = url[len(prefix):]
        self.calls.append(path)
        if path not in self.responses:
            raise BridgeError("repository_entry_not_found", 404)
        return self.responses[path]

    async def send(self, method, url, headers, body):
        self.writes.append((method, url, body))
        if url.endswith("/trees"): return {"sha": "e" * 40}
        if url.endswith("/commits"): return {"sha": "f" * 40}
        assert method == "PATCH" and body == {"sha": "f" * 40, "force": False}
        return {"object": {"sha": "f" * 40}}


class SnapshotTests(unittest.TestCase):
    def call(self, fake=None, execute=execute_subprocess, policy=None, **options):
        fake = fake or SnapshotGitHub()
        options = {"program": PROGRAM, "files": [], **options}
        status, body = asyncio.run(handle(request(**options), "Bearer " + KEY,
            config(policy), fake.fetch, execute, fake.send))
        return status, body, fake

    def test_single_file_compatibility_and_no_host_input_in_root(self):
        for execute in (execute_inline, execute_subprocess):
            status, body, _ = self.call(files=["AGENTS.md"], execute=execute)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["result"]["paths"], ["AGENTS.md", PROGRAM])

    def test_directory_and_multiple_subtrees_preserve_structure_in_both_adapters(self):
        for execute in (execute_inline, execute_subprocess):
            for selectors in (["docs/"], ["docs/", "fables/"]):
                status, body, _ = self.call(files=selectors, execute=execute)
                self.assertEqual(status, 200, body)
                expected = [PROGRAM, "docs/a.md", "docs/nested/b.md", "docs/later.md"]
                if "fables/" in selectors: expected.append("fables/f.md")
                self.assertEqual(body["result"]["paths"], sorted(expected))
                self.assertEqual(body["source"]["snapshot"]["files"], len(expected))

    def test_overlapping_selectors_deduplicate_files(self):
        status, body, fake = self.call(files=["docs/", "docs/nested/", "docs/a.md"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"]["snapshot"]["files"], 4)
        self.assertEqual(sum("/git/blobs/" in c for c in fake.calls), 4)

    def test_materialized_baseline_larger_than_previous_execution_limit(self):
        files = {PROGRAM: SCAN, **{f"docs/{i}/x.md": b"x" for i in range(80)}}
        for execute in (execute_inline, execute_subprocess):
            status, body, _ = self.call(fake=SnapshotGitHub(current=files), files=["docs/"], execute=execute)
            self.assertEqual(status, 200, body)
            self.assertEqual(len(body["result"]["paths"]), 81)

    def test_large_directory_files_execute_unchanged_in_both_adapters(self):
        big = b"x" * (2 * 1024 * 1024)
        for execute in (execute_inline, execute_subprocess):
            status, body, fake = self.call(fake=SnapshotGitHub(current={PROGRAM: SCAN, "docs/big.png": big}),
                files=["docs/"], execute=execute)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["source"]["snapshot"]["bytes"], len(SCAN) + len(big))
            self.assertEqual(fake.writes, [])

    def test_explicit_file_limit_stays_small_even_in_mixed_requests(self):
        for selectors in (["docs/big.png"], ["docs/", "docs/big.png"]):
            fake = SnapshotGitHub(current={PROGRAM: SCAN, "docs/big.png": b"x" * (512 * 1024 + 1)})
            status, body, _ = self.call(fake=fake, files=selectors)
            self.assertEqual((status, body["error"]["code"]), (413, "file_too_large"))
            self.assertFalse(any("/git/blobs/" in c for c in fake.calls))

    def test_directory_file_limit_rejects_before_download(self):
        fake = SnapshotGitHub(current={PROGRAM: SCAN, "docs/big.png": b"x" * (4 * 1024 * 1024 + 1)})
        status, body, _ = self.call(fake=fake, files=["docs/"])
        self.assertEqual((status, body["error"]["code"]), (413, "file_too_large"))
        self.assertFalse(any("/git/blobs/" in c for c in fake.calls))

    def test_changed_large_file_is_rejected_with_zero_writes(self):
        # Small request, large output: must not inherit the directory read grant.
        program = SCAN.replace(b'path.write_text(text, encoding="utf-8")',
                              b'path.write_text(text * (512 * 1024 + 1), encoding="utf-8")')
        fake = SnapshotGitHub(current={PROGRAM: program, "docs/big.txt": b"x" * (512 * 1024 + 1)})
        status, body, _ = self.call(fake=fake, files=["docs/"], input={"changes": {"docs/big.txt": "y"}},
            write={"expected_commit": HEAD, "message": "oversized"})
        self.assertEqual((status, body["error"]["code"]), (413, "file_too_large"))
        self.assertEqual(fake.writes, [])

    def test_large_baseline_allows_small_atomic_write(self):
        fake = SnapshotGitHub(current={PROGRAM: SCAN, "docs/big.png": b"x" * (2 * 1024 * 1024), "docs/a.md": b"old"})
        status, body, _ = self.call(fake=fake, files=["docs/"], input={"changes": {"docs/a.md": "new"}},
            write={"expected_commit": HEAD, "message": "small edit"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["write"]["changed"], ["docs/a.md"])
        self.assertEqual(len(fake.writes), 3)

    def test_new_capacity_exceeds_measured_inventory_with_bounded_headroom(self):
        from bridge.core import MAX_SNAPSHOT_FILES, MAX_SNAPSHOT_TOTAL, MAX_SNAPSHOT_DIRS, MAX_TREE_ENTRIES
        # Exercise a manifest larger than the measured corpus without retaining
        # hundreds of MiB or producing a result larger than the result cap.
        from bridge.core import Execution
        count = 16000
        content = {PROGRAM: SCAN, **{f"docs/{i // 2}/x{i}.md": b"x" for i in range(count)}}
        fake = SnapshotGitHub(current=content)
        async def archive(repo, sha, headers, entries):
            return {p: content[p] for p in entries}
        status, body = asyncio.run(handle(request(program=PROGRAM, files=["docs/"]),
            "Bearer " + KEY, config(), fake.fetch,
            lambda files, program, input: Execution({"count": len(files)}, {}), fake.send, archive))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["count"], count + 1)
        self.assertGreaterEqual(MAX_SNAPSHOT_FILES, 2 * 15455)
        self.assertGreaterEqual(MAX_SNAPSHOT_TOTAL, 2 * 179647667)
        self.assertGreaterEqual(MAX_SNAPSHOT_DIRS, 2 * 6292)
        self.assertGreaterEqual(MAX_TREE_ENTRIES, MAX_SNAPSHOT_FILES + MAX_SNAPSHOT_DIRS)

    def test_historical_snapshot_is_immutable_and_reports_commit(self):
        status, body, _ = self.call(ref=OLD, files=["docs/"], input={"read": "docs/a.md"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"]["commit"], OLD)
        self.assertEqual(body["result"]["text"], "historical")
        self.assertNotIn("docs/later.md", body["result"]["paths"])

    def test_code_overlay_allows_later_canonical_program_on_old_data(self):
        fake = SnapshotGitHub(old={"docs/a.md": b"historical"})
        status, body, _ = self.call(fake=fake, ref=OLD, program_ref="main", files=["docs/"], input={"read": "docs/a.md"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"]["commit"], OLD)
        self.assertEqual(body["source"]["program_commit"], HEAD)
        self.assertEqual(body["result"], {"paths": ["docs/a.md", PROGRAM], "text": "historical"})

    def test_history_requires_read_all_and_named_refs_remain_allowlisted(self):
        narrow = {**POLICY, "read_all": False, "data_prefixes": ["docs"]}
        for options, policy in [({"ref": OLD}, narrow), ({"ref": "tag/not-allowed"}, POLICY),
                                ({"ref": "A" * 40}, POLICY), ({"ref": "../escape"}, POLICY),
                                ({"program_ref": "unknown"}, POLICY)]:
            status, body, fake = self.call(policy=policy, **options)
            self.assertEqual((status, body["error"]["code"]), (403, "ref_not_allowed"))
            self.assertEqual(fake.calls, [])

    def test_nonexistent_or_mismatching_commit_never_executes(self):
        for mismatch in (False, True):
            fake = SnapshotGitHub()
            if mismatch: fake.responses["/git/commits/" + OLD]["sha"] = HEAD
            status, body, _ = self.call(fake=fake, ref=OLD if mismatch else "9" * 40)
            self.assertNotEqual(status, 200)
            self.assertEqual(body["error"]["code"], "invalid_upstream_response" if mismatch else "repository_entry_not_found")
            self.assertFalse(any("/git/blobs/" in c for c in fake.calls))

    def test_immutable_write_and_code_overlay_write_never_reach_repository(self):
        for options in ({"ref": OLD}, {"program_ref": "main"}):
            status, body, fake = self.call(write={"expected_commit": HEAD, "message": "denied"}, **options)
            self.assertEqual(status, 403, body)
            self.assertEqual(fake.calls, [])
            self.assertEqual(fake.writes, [])

    def test_selectors_keep_path_boundary_and_narrow_read_scope(self):
        for path in ["../", "/tmp/", "docs/../../", "docs//", "C:/", ".git/", "docs/./"]:
            status, body, fake = self.call(files=[path])
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_path"), path)
            self.assertEqual(fake.calls, [])
        status, body, _ = self.call(files=["fables/"], policy={**POLICY, "read_all": False, "data_prefixes": ["docs"]})
        self.assertEqual((status, body["error"]["code"]), (403, "file_not_allowed"))

    def test_directory_skips_symlink_submodule_and_descendants_explicit_entries_fail(self):
        extras = [{"path": "link", "type": "blob", "mode": "120000", "sha": "3" * 40},
                  {"path": "module", "type": "commit", "mode": "160000", "sha": "4" * 40},
                  {"path": "link/child", "type": "blob", "mode": "100644", "sha": "5" * 40, "size": 1}]
        status, body, fake = self.call(fake=SnapshotGitHub(extras=extras), files=["docs/"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"]["snapshot"]["skipped_entries"], 3)
        self.assertFalse(any("link" in p or "module" in p for p in body["result"]["paths"]))
        for selector in ["docs/link", "docs/link/", "docs/module", "docs/module/"]:
            status, body, _ = self.call(fake=SnapshotGitHub(extras=extras), files=[selector])
            self.assertEqual(body["error"]["code"], "unsupported_repository_entry")

    def test_limits_and_truncated_tree_fail_before_blob_download(self):
        for name, value, error in [("MAX_SNAPSHOT_FILES", 2, "too_many_snapshot_files"),
                                   ("MAX_SNAPSHOT_TOTAL", 1, "snapshot_too_large"),
                                   ("MAX_SNAPSHOT_DIRS", 1, "too_many_snapshot_directories"),
                                   ("MAX_TREE_ENTRIES", 1, "too_many_snapshot_entries"),
                                   ("MAX_FILE", 1, "file_too_large")]:
            with patch("bridge.core." + name, value):
                status, body, fake = self.call(files=["docs/"])
            self.assertEqual((status, body["error"]["code"]), (413, error), name)
            self.assertFalse(any("/git/blobs/" in c for c in fake.calls))
        fake = SnapshotGitHub(); fake.responses["/git/trees/" + fake.docs_tree + "?recursive=1"]["truncated"] = True
        status, body, _ = self.call(fake=fake, files=["docs/"])
        self.assertEqual((status, body["error"]["code"]), (413, "repository_tree_truncated"))
        self.assertFalse(any("/git/blobs/" in c for c in fake.calls))

    def test_directory_write_keeps_single_commit_and_expected_commit_guard(self):
        status, body, fake = self.call(files=["docs/"], input={"changes": {"docs/a.md": "updated", "docs/nested/b.md": None, "docs/new.md": "new"}}, write={"expected_commit": HEAD, "message": "atomic"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["write"]["changed"], ["docs/a.md", "docs/nested/b.md", "docs/new.md"])
        self.assertEqual([w[0] for w in fake.writes], ["POST", "POST", "PATCH"])
        status, body, fake = self.call(files=["docs/"], write={"expected_commit": OLD, "message": "stale"})
        self.assertEqual((status, body["error"]["code"]), (409, "branch_conflict"))
        self.assertEqual(fake.writes, [])

    def test_mcp_schema_and_execution_accept_directories_and_program_ref(self):
        fake = SnapshotGitHub(old={"docs/a.md": b"historical"})
        server = create_server(config(), StaticTokenVerifier(tokens={AUTH: {"client_id": "test", "scopes": []}}), fetch=fake.fetch, send=fake.send)
        with TestClient(server.http_app(path="/mcp", stateless_http=True, json_response=True)) as client:
            listing = rpc(client, "tools/list").json()["result"]["tools"]
            schema = next(t for t in listing if t["name"] == "run_readonly_skill")["inputSchema"]
            self.assertIn("program_ref", schema["properties"])
            self.assertNotIn("program_ref", schema["required"])
            self.assertIn("historical data ref", schema["properties"]["program_ref"]["description"])
            write_schema = next(t for t in listing if t["name"] == "run_write_skill")["inputSchema"]
            self.assertNotIn("program_ref", write_schema["properties"])
            targets = rpc(client, "tools/call", {"name": "list_runtime_targets", "arguments": {}}).json()["result"]["structuredContent"]
            self.assertEqual(targets["runtime_version"], "0.4.0")
            limits = targets["snapshot_usage"]["limits"]
            self.assertEqual(limits["directory_files"], 32768)
            self.assertEqual(limits["directory_bytes"], 384 * 1024 * 1024)
            self.assertEqual(limits["directory_file_bytes"], 4 * 1024 * 1024)
            self.assertEqual(limits["file_bytes"], 512 * 1024)
            result = rpc(client, "tools/call", {"name": "run_readonly_skill", "arguments": {"repository": "owner/private", "ref": OLD, "program_ref": "main", "program": PROGRAM, "files": ["docs/"], "input": {}}}).json()["result"]
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(result["structuredContent"]["result"]["paths"], ["docs/a.md", PROGRAM])


class ArchiveTests(unittest.TestCase):
    def archive(self, members):
        import io
        import tarfile
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w:gz") as tar:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type = tarfile.SYMTYPE; info.linkname = "/etc/passwd"
                    tar.addfile(info)
                else:
                    info.size = len(content); tar.addfile(info, io.BytesIO(content))
        result.seek(0)
        return result

    def test_archive_materializes_only_selected_regular_files(self):
        from bridge.http import read_archive
        stream = self.archive([("repo/docs/a.md", b"data", "file"),
            ("repo/other.txt", b"not selected", "file"), ("repo/link", b"", "link")])
        self.assertEqual(read_archive(stream, {"docs/a.md": {"size": 4}}), {"docs/a.md": b"data"})

    def test_archive_accepts_large_normal_file_and_keeps_independent_byte_cap(self):
        from bridge.http import read_archive
        content = b"x" * (2 * 1024 * 1024)
        result = read_archive(self.archive([("repo/docs/big.png", content, "file")]),
                              {"docs/big.png": {"size": len(content)}})
        self.assertEqual(result["docs/big.png"], content)
        # Compressed input is tiny; the expanded-byte limit must still hold.
        with patch("bridge.core.MAX_ARCHIVE_BYTES", 1024 * 1024), self.assertRaisesRegex(BridgeError, "archive_too_large"):
            read_archive(self.archive([("repo/docs/big.png", content, "file")]),
                         {"docs/big.png": {"size": len(content)}})

    def test_archive_rejects_escape_links_duplicates_missing_or_oversized_data(self):
        from bridge.http import read_archive
        for members, entries in [
            ([("repo/../escape", b"x", "file")], {}),
            ([("/absolute", b"x", "file")], {}),
            ([("repo/docs/a.md", b"", "link")], {"docs/a.md": {"size": 0}}),
            ([("repo/docs/a.md", b"x", "file")] * 2, {"docs/a.md": {"size": 1}}),
            ([("repo/other.txt", b"x", "file")], {"docs/a.md": {"size": 1}})]:
            with self.subTest(members=members), self.assertRaises(BridgeError):
                read_archive(self.archive(members), entries)
        with patch("bridge.core.MAX_ARCHIVE_BYTES", 10), self.assertRaisesRegex(BridgeError, "archive_too_large"):
            read_archive(self.archive([("repo/a", b"x" * 1000, "file")]), {"a": {"size": 1000}})

    def test_large_snapshot_uses_one_archive_and_verifies_every_blob(self):
        content = {PROGRAM: SCAN, **{f"docs/{i}.md": b"x" for i in range(128)}}
        for corrupt in (False, True):
            fake = SnapshotGitHub(current=content)
            archives = []
            async def archive(repo, sha, headers, entries):
                archives.append((repo, sha))
                return {p: b"bad" if corrupt else content[p] for p in entries}
            status, body = asyncio.run(handle(request(program=PROGRAM, files=["docs/"]),
                "Bearer " + KEY, config(), fake.fetch, execute_subprocess, fake.send, archive))
            self.assertEqual(archives, [("owner/private", HEAD)])
            self.assertFalse(any("/git/blobs/" in c for c in fake.calls))
            if corrupt:
                self.assertEqual((status, body["error"]["code"]), (502, "invalid_upstream_response"))
            else:
                self.assertEqual(status, 200, body)
                self.assertEqual(len(body["result"]["paths"]), 129)


if __name__ == "__main__":
    unittest.main()
