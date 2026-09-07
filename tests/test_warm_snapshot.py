"""Core + real HTTP transport reuse under a changing mock GitHub main branch.

The small executable Pulse-shaped fixture checks materialized navigation output;
this exercises transport integration, not production Recall's semantic quality.
No request leaves the mocked urllib opener.
"""
import asyncio
from copy import deepcopy
import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from bridge.core import handle
from bridge.execution import execute_inline
from bridge.http import fetch_json, transport_status
from bridge.transport_cache import TransportState
from test_bridge import KEY, request
from test_snapshot_efficiency import NAV, POLICY, RECALL, sources
from test_snapshots import SnapshotGitHub, config, HEAD, OLD

PULSE = b'''from pathlib import Path
CANONICAL_DEPENDENCIES = []
def run(root, input):
    if input["action"] != "pulse":
        raise ValueError("unexpected action")
    return {"story": (Path(root) / "memory/story/STORIES.md").read_text(encoding="utf-8")}
'''


class WarmSnapshotTests(unittest.TestCase):
    def test_cold_warm_pulse_and_mutable_main_advance(self):
        current = {**sources(), RECALL: PULSE, NAV[0]: b"current: new decision"}
        old = {**current, NAV[0]: b"previous: earlier decision"}
        github = SnapshotGitHub(current=current, old=old)
        github.responses["/git/ref/heads/main"] = {"object": {"sha": OLD}}
        upstream, lock = [], threading.Lock()
        state = TransportState()

        def open_request(req, **kwargs):
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.get_header("Authorization"), "Bearer private-credential")
            prefix = "https://api.github.com/repos/owner/private"
            self.assertTrue(req.full_url.startswith(prefix + "/"))
            path = req.full_url[len(prefix):]
            with lock:
                upstream.append(path)
                payload = deepcopy(github.responses[path])
                remaining = 5000 - len(upstream)
            # The shared snapshot fixture omits response SHA on trees/blobs;
            # real GitHub includes it, and the HTTP cache validates it.
            if not path.startswith("/git/ref/"):
                payload["sha"] = urlsplit(path).path.rsplit("/", 1)[-1]
            result = io.BytesIO(json.dumps(payload).encode())
            result.status = 200
            result.headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": str(remaining)}
            return result

        def pulse():
            before = len(upstream)
            status, result = asyncio.run(handle(
                request(program=RECALL, files=NAV, input={"action": "pulse"}),
                "Bearer " + KEY, config(POLICY), fetch_json, execute_inline))
            self.assertEqual(status, 200, result)
            return result, len(upstream) - before

        opener = MagicMock()
        opener.open.side_effect = open_request
        with patch("bridge.http._transport", state), patch("bridge.http.build_opener", return_value=opener):
            cold, cold_requests = pulse()
            warm, warm_requests = pulse()
            self.assertEqual(cold["result"], {"story": "previous: earlier decision"})
            self.assertEqual(warm["result"], cold["result"])
            self.assertEqual((cold["source"]["commit"], warm["source"]["commit"]), (OLD, OLD))
            self.assertEqual(cold_requests, 15)
            self.assertEqual(warm_requests, 1)
            self.assertEqual(upstream[-1], "/git/ref/heads/main")
            self.assertEqual(transport_status("private-credential")["cache_hits"], 14)

            # Advance mutable main while all immutable old objects remain cached.
            github.responses["/git/ref/heads/main"] = {"object": {"sha": HEAD}}
            advanced, advanced_requests = pulse()
            self.assertEqual(advanced["source"]["commit"], HEAD)
            self.assertEqual(advanced["result"], {"story": "current: new decision"})
            self.assertIn("/git/commits/" + HEAD, upstream[-advanced_requests:])
            self.assertGreater(advanced_requests, warm_requests)
            self.assertLess(advanced_requests, cold_requests)

            current_warm, current_warm_requests = pulse()
            self.assertEqual(current_warm["source"]["commit"], HEAD)
            self.assertEqual(current_warm["result"], advanced["result"])
            self.assertEqual(current_warm_requests, 1)
            self.assertEqual(upstream.count("/git/ref/heads/main"), 4)
            stats = transport_status("private-credential")
            self.assertEqual(stats["upstream_requests"], len(upstream))
            self.assertEqual(stats["remaining"], 5000 - len(upstream))
        self.assertEqual(opener.open.call_count, cold_requests + warm_requests + advanced_requests + current_warm_requests)


if __name__ == "__main__":
    unittest.main()
