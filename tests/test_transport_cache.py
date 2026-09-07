"""Immutable reuse, per-credential quota state, and cross-loop coordination."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from bridge.core import BridgeError
from bridge.http import send_json, fetch_archive, transport_status
from bridge.transport_cache import TransportState, credential_key, immutable_key

SHA = "a" * 40
URL = "https://api.github.com/repos/owner/private/git/trees/" + SHA
HEADERS = {"Authorization": "Bearer private-token", "User-Agent": "bridge"}


def response(value=None, headers=None):
    result = io.BytesIO(json.dumps(value or {"sha": SHA, "tree": []}).encode())
    result.status, result.headers = 200, headers or {}
    return result


class TransportCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.state = TransportState(clock=lambda: self.now, wall_clock=lambda: self.now)
        self.state_patch = patch("bridge.http._transport", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def get(self, url=URL, headers=HEADERS):
        return asyncio.run(send_json("GET", url, headers))

    def test_only_exact_immutable_git_object_gets_have_keys(self):
        for kind in ("commits", "trees", "blobs"):
            self.assertIsNotNone(immutable_key("GET", URL.replace("trees", kind), HEADERS))
        self.assertNotEqual(immutable_key("GET", URL, HEADERS),
                            immutable_key("GET", URL + "?recursive=1", HEADERS))
        for method, url, body in [
            ("POST", URL, {}), ("GET", URL, {}),
            ("GET", URL.replace(SHA, "main"), None),
            ("GET", URL.replace("git/trees/" + SHA, "git/ref/heads/main"), None),
            ("GET", URL.replace("git/trees/" + SHA, "issues/1"), None),
            ("GET", URL.replace("git/trees/" + SHA, "pulls/1"), None),
            ("GET", URL + "?other=1", None), ("GET", URL + "#fragment", None),
            ("GET", URL.replace("api.github.com", "other.example"), None),
            ("GET", URL.replace("https:", "http:"), None),
        ]:
            with self.subTest(method=method, url=url):
                self.assertIsNone(immutable_key(method, url, HEADERS, body))

    def test_cache_separates_credentials_repositories_and_representations(self):
        opener = MagicMock()
        opener.open.side_effect = lambda *a, **k: response()
        with patch("bridge.http.build_opener", return_value=opener):
            original = self.get()
            original["tree"].append("caller mutation")
            self.assertEqual(self.get()["tree"], [])
            self.get(headers={**HEADERS, "Authorization": "Bearer another-token"})
            self.get(url=URL.replace("owner/private", "other/private"))
            self.get(headers={**HEADERS, "Accept": "different-representation"})
            self.get(url=URL + "?recursive=1")
        self.assertEqual(opener.open.call_count, 5)
        stats = transport_status("private-token")
        self.assertEqual(stats["scope"], "process")
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["upstream_requests"], 4)
        self.assertNotIn("private", json.dumps(stats))

    def test_mutable_reads_and_writes_never_reuse(self):
        opener = MagicMock()
        opener.open.side_effect = lambda *a, **k: response()
        with patch("bridge.http.build_opener", return_value=opener):
            for _ in range(2):
                self.get(URL.replace("git/trees/" + SHA, "git/ref/heads/main"))
                asyncio.run(send_json("POST", URL, HEADERS, {}))
        self.assertEqual(opener.open.call_count, 4)

    def test_lru_bytes_entry_limit_and_ttl(self):
        state = TransportState(max_bytes=6, max_entries=2, ttl=5, clock=lambda: self.now)
        calls = []
        def read(key):
            return state.get_or_fetch(key, b"credential", lambda: calls.append(key) or b"abc")
        read("a"); read("b"); read("a"); read("c"); read("b")
        self.assertEqual(calls, ["a", "b", "c", "b"])
        self.assertLessEqual(state._bytes, 6)
        self.now += 6
        read("b")
        self.assertEqual(calls[-2:], ["b", "b"])
        self.assertEqual(len(state._cache), 1)
        state.get_or_fetch("oversize", b"credential", lambda: b"0123456789")
        self.assertNotIn("oversize", state._cache)

    def test_invalid_json_and_upstream_errors_are_not_cached(self):
        invalid = io.BytesIO(b"not json")
        invalid.status, invalid.headers = 200, {}
        error = HTTPError(URL, 500, "error", {}, io.BytesIO(b"private body"))
        opener = MagicMock()
        opener.open.side_effect = [invalid, error, response()]
        with patch("bridge.http.build_opener", return_value=opener):
            for _ in range(2):
                with self.assertRaisesRegex(BridgeError, "github_request_failed"):
                    self.get()
            self.get()
            self.get()
        self.assertEqual(opener.open.call_count, 3)

    def test_invalid_object_responses_never_enter_immutable_cache(self):
        opener = MagicMock()
        opener.open.side_effect = [response({"message": "not an object"}),
                                   response({"sha": "b" * 40}), response()]
        with patch("bridge.http.build_opener", return_value=opener):
            for _ in range(2):
                with self.assertRaisesRegex(BridgeError, "invalid_upstream_response"):
                    self.get()
            self.get()
            self.get()
        self.assertEqual(opener.open.call_count, 3)

    def test_singleflight_across_threads_and_asyncio_run_loops(self):
        entered, release = threading.Event(), threading.Event()
        def open_request(*a, **k):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release timeout")
            return response()
        opener = MagicMock()
        opener.open.side_effect = open_request
        with patch("bridge.http.build_opener", return_value=opener), ThreadPoolExecutor(max_workers=8) as pool:
            results = [pool.submit(self.get) for _ in range(8)]
            self.assertTrue(entered.wait(2))
            deadline = time.monotonic() + 3
            while self.state.status(credential_key(HEADERS))["coalesced"] < 7 and time.monotonic() < deadline:
                threading.Event().wait(.005)
            release.set()
            self.assertEqual([r.result(timeout=5)["sha"] for r in results], [SHA] * 8)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(transport_status(HEADERS)["coalesced"], 7)
        self.assertEqual(self.state._flights, {})

    def test_singleflight_failure_does_not_survive_retry(self):
        calls = []
        def fail():
            calls.append(1)
            raise BridgeError("github_request_failed", 502)
        with self.assertRaises(BridgeError):
            self.state.get_or_fetch("key", b"credential", fail)
        self.assertEqual(self.state._flights, {})
        self.assertEqual(self.state.get_or_fetch("key", b"credential", lambda: b"ok"), b"ok")

    def test_process_parallel_cap_and_waiting_requests_recheck_cooldown(self):
        state = TransportState(max_parallel=2, clock=lambda: self.now, wall_clock=lambda: self.now)
        lock, release = threading.Lock(), threading.Event()
        two_entered = threading.Event()
        active, peak, started = 0, 0, 0
        def work():
            nonlocal active, peak, started
            with state.upstream(b"credential"):
                with lock:
                    active += 1
                    started += 1
                    peak = max(peak, active)
                    if active == 2:
                        two_entered.set()
                release.wait(5)
                with lock:
                    active -= 1
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(work) for _ in range(8)]
            self.assertTrue(two_entered.wait(2))
            state.observe(b"credential", "rest", 403, {"Retry-After": "60"},
                          BridgeError("github_rate_limited", 429, retry_after=60))
            release.set()
            failures = 0
            for future in futures:
                try:
                    future.result(timeout=5)
                except BridgeError:
                    failures += 1
        self.assertEqual((peak, started, failures), (2, 2, 6))

    def test_rate_limit_failfast_is_credential_scoped_and_expires(self):
        error = HTTPError(URL, 403, "error", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, io.BytesIO(b'{}'))
        opener = MagicMock()
        opener.open.side_effect = [error, response(), response()]
        with patch("bridge.http.build_opener", return_value=opener):
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                self.get()
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                self.get(URL.replace(SHA, "b" * 40))
            self.assertEqual(opener.open.call_count, 1)
            self.get(headers={**HEADERS, "Authorization": "Bearer other"})
            self.assertEqual(transport_status(HEADERS)["cooldown_until"], 1100)
            self.now = 1101
            self.get()
        self.assertEqual(opener.open.call_count, 3)
        self.assertIsNone(transport_status(HEADERS)["cooldown_until"])

    def test_successful_last_quota_response_blocks_next_request_and_is_observable(self):
        opener = MagicMock()
        opener.open.return_value = response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "1100"})
        with patch("bridge.http.build_opener", return_value=opener):
            self.assertEqual(self.get()["sha"], SHA)
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                self.get(URL.replace(SHA, "b" * 40))
        self.assertEqual(opener.open.call_count, 1)
        stats = transport_status(HEADERS)
        self.assertEqual((stats["remaining"], stats["limit"], stats["reset_at"]), (0, 5000, 1100))

    def test_secondary_without_headers_has_bounded_cooldown_and_no_secret_logs(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError(URL, 429, "secret", {}, io.BytesIO(b"private body"))
        with patch("bridge.http.build_opener", return_value=opener), self.assertLogs("bridge.github_transport", "INFO") as logs:
            with self.assertRaises(BridgeError):
                self.get()
        self.assertEqual(transport_status(HEADERS)["cooldown_until"], 1060)
        rendered = " ".join(logs.output)
        for secret in ("private-token", "private body", "api.github.com", "owner/private", SHA):
            self.assertNotIn(secret, rendered)

    def test_secondary_retry_after_is_not_extended_to_primary_reset(self):
        error = HTTPError(URL, 403, "secondary", {"X-RateLimit-Remaining": "4000",
                          "X-RateLimit-Reset": "4600", "Retry-After": "30"}, io.BytesIO())
        opener = MagicMock()
        opener.open.side_effect = [error, response()]
        with patch("bridge.http.build_opener", return_value=opener):
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                self.get()
            self.assertEqual(transport_status(HEADERS)["cooldown_until"], 1030)
            self.now = 1029
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                self.get()
            self.assertEqual(opener.open.call_count, 1)
            self.now = 1031
            self.get()
        self.assertEqual(opener.open.call_count, 2)

    def test_primary_exhaustion_still_waits_until_reset(self):
        self.state.observe(credential_key(HEADERS), "rest", 403,
                           {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600", "Retry-After": "30"},
                           BridgeError("github_rate_limited", 429, retry_after=30, reset_at=4600))
        self.assertEqual(transport_status(HEADERS)["cooldown_until"], 4600)

    def test_archive_redirect_is_observed_without_forwarding_token(self):
        redirect = HTTPError(URL, 302, "redirect", {"Location": "https://codeload.github.com/owner/private/legacy.tar.gz/" + SHA,
                             "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, io.BytesIO())
        opener = MagicMock()
        opener.open.side_effect = [redirect, response()]
        with patch("bridge.http.build_opener", return_value=opener), patch("bridge.http.read_archive", return_value={}):
            self.assertEqual(asyncio.run(fetch_archive("owner/private", SHA, HEADERS, {})), {})
            redirected_request = opener.open.call_args_list[1].args[0]
            self.assertIsNone(redirected_request.get_header("Authorization"))
            with self.assertRaisesRegex(BridgeError, "github_rate_limited"):
                asyncio.run(fetch_archive("owner/private", SHA, HEADERS, {}))
        self.assertEqual(opener.open.call_count, 2)
        stats = transport_status(HEADERS)
        self.assertEqual((stats["upstream_requests"], stats["remaining"], stats["reset_at"]), (2, 0, 1100))


if __name__ == "__main__":
    unittest.main()
