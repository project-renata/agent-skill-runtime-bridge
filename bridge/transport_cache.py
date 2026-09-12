"""Process-local immutable-object reuse and GitHub request coordination.

Only credential fingerprints live in keys. Locks and Futures work across the
short-lived asyncio loops used by the MCP adapter; no loop owns this state.
"""
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
import hashlib
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from .core import BridgeError
from .request_budget import spend_request

_LOG = logging.getLogger("bridge.github_transport")
_OBJECT = re.compile(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/git/(commits|trees|blobs)/([0-9a-f]{40})")


def credential_key(headers):
    authorization = next((str(v) for k, v in headers.items() if k.lower() == "authorization"), "")
    return hashlib.sha256(authorization.encode()).digest()


def immutable_key(method, url, headers, body=None):
    """Branches, mutable REST resources, writes and arbitrary queries never cache."""
    parts = urlsplit(url)
    match = _OBJECT.fullmatch(parts.path)
    if (method != "GET" or body is not None or parts.scheme != "https"
            or parts.netloc != "api.github.com" or parts.fragment or not match
            or (parts.query and not (match[1] == "trees" and parts.query == "recursive=1"))):
        return None
    normalized = {k.lower(): str(v) for k, v in headers.items()}
    return (credential_key(headers), url, normalized.get("accept", ""),
            normalized.get("x-github-api-version", ""))


def request_kind(url):
    path = urlsplit(url).path
    match = _OBJECT.fullmatch(path)
    if match:
        return "git_" + match[1]
    if "/tarball/" in path or urlsplit(url).netloc == "codeload.github.com":
        return "archive"
    if "/git/ref" in path:
        return "git_ref"
    return "rest"


def _numeric(headers, name):
    value = headers.get(name, "")
    return int(value) if value.isdigit() and len(value) <= 12 else None


class TransportState:
    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_entries=1024,
                 ttl=120, max_parallel=4, clock=time.monotonic, wall_clock=time.time):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._cache, self._flights, self._cooldowns = OrderedDict(), {}, OrderedDict()
        self._stats = OrderedDict()
        self._bytes = 0

    def _stat(self, credential):
        if credential not in self._stats:
            self._stats[credential] = {"upstream_requests": 0, "cache_hits": 0,
                                       "coalesced": 0, "remaining": None,
                                       "limit": None, "reset_at": None}
        self._stats.move_to_end(credential)
        while len(self._stats) > 256:
            self._stats.popitem(last=False)
        return self._stats[credential]

    def started(self, credential):
        spend_request()
        with self._lock:
            self._stat(credential)["upstream_requests"] += 1

    def status(self, credential):
        with self._lock:
            self._prune()
            result = dict(self._stat(credential))
            blocked = self._cooldowns.get(credential)
            result.update(scope="process", cooldown_until=(
                int(self.wall_clock() + max(0, blocked[0] - self.clock())) if blocked else None))
            return result

    def _prune(self):
        now = self.clock()
        for key, (deadline, raw) in list(self._cache.items()):
            if deadline <= now:
                self._bytes -= len(raw)
                del self._cache[key]
        for key, (deadline, _) in list(self._cooldowns.items()):
            if deadline <= now:
                del self._cooldowns[key]

    def check(self, credential):
        with self._lock:
            self._prune()
            blocked = self._cooldowns.get(credential)
            if blocked:
                raise BridgeError("github_rate_limited", 429, **blocked[1])

    @contextmanager
    def upstream(self, credential):
        self.check(credential)
        with self._slots:
            # Another in-flight request may have exhausted the budget meanwhile.
            self.check(credential)
            yield

    def observe(self, credential, kind, status, headers, error=None):
        """Record only numeric quota metadata; never log URLs, bodies or keys."""
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        remaining = _numeric(normalized, "x-ratelimit-remaining")
        limit = _numeric(normalized, "x-ratelimit-limit")
        reset = _numeric(normalized, "x-ratelimit-reset")
        retry = _numeric(normalized, "retry-after")
        with self._lock:
            stats = self._stat(credential)
            for name, value in (("remaining", remaining), ("limit", limit), ("reset_at", reset)):
                if value is not None:
                    stats[name] = value
        limited = error is not None and error.code == "github_rate_limited"
        if limited or remaining == 0:
            wall_now = self.wall_clock()
            # A primary reset belongs to the hourly bucket. For secondary
            # throttling with quota remaining, it must not extend Retry-After.
            primary_delay = reset - wall_now if remaining == 0 and reset is not None else None
            delays = [value for value in (primary_delay, retry)
                      if value is not None and value > 0]
            # Secondary throttles may omit reset headers. A bounded local cooldown
            # prevents immediate amplification without retrying or sleeping.
            delay = max(delays) if delays else 60
            details = dict(error.details) if limited else {"upstream_status": status}
            if reset is not None:
                details["reset_at"] = reset
            if retry is not None:
                details["retry_after"] = retry
            if reset is None and retry is None:
                details["retry_after"] = 60
            deadline = self.clock() + delay
            with self._lock:
                self._prune()
                previous = self._cooldowns.get(credential)
                if previous is None or previous[0] < deadline:
                    self._cooldowns[credential] = (deadline, details)
                self._cooldowns.move_to_end(credential)
                while len(self._cooldowns) > 256:
                    self._cooldowns.popitem(last=False)
        _LOG.info("github_upstream kind=%s status=%s remaining=%s reset=%s retry_after=%s",
                  kind, status, remaining, reset, retry)

    def get_or_fetch(self, key, credential, fetch):
        self.check(credential)
        if key is None:
            return fetch()
        with self._lock:
            self._prune()
            cached = self._cache.get(key)
            if cached:
                self._cache.move_to_end(key)
                self._stat(credential)["cache_hits"] += 1
                return cached[1]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = Future()
            else:
                self._stat(credential)["coalesced"] += 1
        if not leader:
            return flight.result()
        try:
            raw = fetch()
            with self._lock:
                if len(raw) <= self.max_bytes and self.max_entries > 0:
                    self._cache[key] = (self.clock() + self.ttl, raw)
                    self._bytes += len(raw)
                    while self._bytes > self.max_bytes or len(self._cache) > self.max_entries:
                        _, (_, removed) = self._cache.popitem(last=False)
                        self._bytes -= len(removed)
            flight.set_result(raw)
            return raw
        except BaseException as error:
            # Deliver a failure only to current waiters, never retain it as cache.
            flight.set_exception(error)
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
