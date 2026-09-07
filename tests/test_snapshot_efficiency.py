"""Bound GitHub read amplification without fetching a live repository."""
import asyncio
import base64
from collections import Counter
import json
import unittest

from bridge.core import Execution, handle
from test_bridge import KEY, request
from test_snapshots import SnapshotGitHub, config, HEAD, OLD


RECALL = "memory/skill/lifecycle/recall/scripts/recall.py"
REMEMBER = "memory/skill/lifecycle/remember/scripts/remember.py"
WRITER = "memory/skill/creators/story-writer/scripts/story_writer.py"
QUALITY = "memory/skill/quality/story-quality/scripts/story_quality.py"
EXPRESSION = "memory/skill/quality/expression-quality/scripts/expression_quality.py"
MEASURE = "memory/skill/shared/attention-budget/scripts/measure.py"
PROFILES = "memory/skill/shared/attention-budget/references/profiles.json"
STORY = "memory/story/system/example/STORY.md"
NAV = ["memory/story/STORIES.md", "memory/fable/work/FABLES.md", "memory/skill/SKILLS.md"]
POLICY = {"ref": "main", "read_all": True, "program_prefixes": ["memory/skill", "program"]}


def code(*dependencies):
    return ("CANONICAL_DEPENDENCIES=" + repr(list(dependencies)) +
            "\ndef run(root,input): return {}\n").encode()


def sources():
    return {"docs/a.md": b"fixture", RECALL: code(), REMEMBER: code(WRITER),
            WRITER: code(MEASURE, QUALITY), QUALITY: code(EXPRESSION, MEASURE),
            EXPRESSION: code(), MEASURE: code(PROFILES), PROFILES: b"{}",
            STORY: b"story", **{p: p.encode() for p in NAV}}


class SnapshotEfficiencyTests(unittest.TestCase):
    def call(self, files, source=None, program=RECALL, old=None, archive_enabled=True, **extra):
        fake = SnapshotGitHub(current=source or sources(), old=old)
        archive_calls = []

        async def archive(repo, sha, headers, entries):
            archive_calls.append((sha, len(entries)))
            return {p: base64.b64decode(fake.responses["/git/blobs/" + e["sha"]]["content"])
                    for p, e in entries.items()}

        status, body = asyncio.run(handle(
            request(program=program, files=files, **extra), "Bearer " + KEY, config(POLICY),
            fake.fetch, lambda loaded, program, data: Execution(
                {"dependency": loaded.get("program/helper.py", b"").decode()}, {}),
            fake.send, archive if archive_enabled else None))
        return status, body, fake, archive_calls

    def test_pulse_read_cost_is_visible_and_does_not_scan_recursive_root(self):
        status, body, fake, archives = self.call(NAV)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(fake.calls), 15)
        diagnostics = body["source"]["snapshot"]["read_diagnostics"]
        self.assertEqual(diagnostics["transport_requests"], 15)
        self.assertEqual(diagnostics["json_requests"], {"ref": 1, "commit": 1, "tree": 9,
                                                      "blob": 4, "other": 0})
        self.assertFalse(any("recursive=1" in p for p in fake.calls))
        self.assertEqual(archives, [])
        self.assertNotIn("private-credential", json.dumps(diagnostics))
        self.assertNotIn("owner/private", json.dumps(diagnostics))

    def test_small_and_boundary_subtrees_have_bounded_request_cost(self):
        costs = {}
        for count in (7, 8, 127, 128):
            with self.subTest(count=count):
                source = {**sources(), **{f"records/{i}.md": str(i).encode() for i in range(count)}}
                status, body, fake, archives = self.call(["records/"], source)
                self.assertEqual(status, 200, body)
                self.assertEqual(body["source"]["snapshot"]["files"], count + 1)
                costs[count] = len(fake.calls) + len(archives)
                self.assertEqual(len(archives), 0 if count == 7 else 1)
                self.assertEqual(sum("/git/blobs/" in p for p in fake.calls), 8 if count == 7 else 1)
                self.assertEqual(body["source"]["snapshot"]["read_diagnostics"]["transport_requests"],
                                 costs[count])
        self.assertEqual(costs[8], 11)
        self.assertEqual(costs[127], 11)
        self.assertEqual(costs[128], 11)

    def test_two_medium_selections_use_two_scoped_archives(self):
        source = {**sources(), **{f"records/{folder}/{i}.md": str(i).encode()
                                 for folder in ("a", "b") for i in range(100)}}
        status, body, fake, archives = self.call(["records/a/", "records/b/"], source)
        self.assertEqual(status, 200, body)
        self.assertEqual([count for _, count in archives], [100, 100])
        self.assertEqual(len(fake.calls) + len(archives), 14)
        root = fake.root_trees[HEAD]
        self.assertNotIn("/git/trees/" + root + "?recursive=1", fake.calls)

    def test_loaded_canonical_dependencies_are_verified_without_second_blob_fetch(self):
        files = [STORY, WRITER, QUALITY, EXPRESSION, MEASURE, PROFILES]
        status, body, fake, archives = self.call(files, program=REMEMBER)
        self.assertEqual(status, 200, body)
        diagnostics = body["source"]["snapshot"]["read_diagnostics"]
        self.assertEqual(diagnostics["reused_dependencies"], 5)
        self.assertEqual(diagnostics["transport_requests"], 30)
        blob_calls = Counter(p for p in fake.calls if "/git/blobs/" in p)
        # Distinct paths can share a Git object; the total is still one fetch per
        # selected path, with no further fetch during dependency hydration.
        self.assertEqual(sum(blob_calls.values()), 7)
        self.assertEqual(archives, [])

    def test_old_data_dependency_is_replaced_from_the_code_revision(self):
        program, helper = "program/main.py", "program/helper.py"
        source = {**sources(), program: code(helper), helper: b"new"}
        old = {**source, helper: b"old"}
        status, body, fake, _ = self.call([helper], source, program=program, old=old,
                                        ref=OLD, program_ref="main")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["dependency"], "new")
        diagnostics = body["source"]["snapshot"]["read_diagnostics"]
        self.assertEqual(diagnostics["reused_dependencies"], 0)
        self.assertEqual(diagnostics["transport_requests"], len(fake.calls))

    def test_dependencies_already_loaded_in_archive_do_not_require_blob_requests(self):
        status, body, fake, archives = self.call([STORY, "memory/skill/"], program=REMEMBER)
        self.assertEqual(status, 200, body)
        diagnostics = body["source"]["snapshot"]["read_diagnostics"]
        self.assertEqual(diagnostics["reused_dependencies"], 5)
        self.assertEqual(len(archives), 1)
        self.assertEqual(sum("/git/blobs/" in p for p in fake.calls), 1)
        self.assertEqual(diagnostics["transport_requests"], 26)

    def test_reused_directory_dependency_keeps_the_smaller_code_size_limit(self):
        program, helper = "program/main.py", "program/helper.py"
        source = {**sources(), program: code(helper), helper: b"x" * (512 * 1024 + 1)}
        status, body, fake, _ = self.call(["program/"], source, program=program)
        self.assertEqual((status, body["error"]["code"]), (413, "file_too_large"))
        self.assertEqual(fake.writes, [])


if __name__ == "__main__":
    unittest.main()
