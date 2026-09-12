"""CPython's GitHub transport. Credentials only reach api.github.com."""
import asyncio
import json
from contextlib import nullcontext
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .core import BridgeError, MAX_FILE, MAX_SNAPSHOT_FILE, MAX_TREE_RESPONSE, MAX_ARCHIVE_BYTES, github_http_error
from .transport_cache import TransportState, credential_key, immutable_key, request_kind
from .github_coordination import coordinator_from_env


_transport = TransportState()


def transport_status(headers_or_token):
    """Safe observations for this process and the explicitly selected credential."""
    headers = ({"Authorization": "Bearer " + headers_or_token} if isinstance(headers_or_token, str)
               and headers_or_token else headers_or_token or {})
    return _transport.status(credential_key(headers))


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def send_json(method, url, headers, body=None):
    credential = credential_key(headers)
    key = immutable_key(method, url, headers, body)
    kind = request_kind(url)
    coordinator = coordinator_from_env()

    def fetch():
        with _transport.upstream(credential), (coordinator.upstream(credential) if coordinator else nullcontext()):
            try:
                data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
                request_headers = {**headers, "Content-Type": "application/json"} if data is not None else headers
                _transport.started(credential)
                with build_opener(NoRedirects).open(Request(url, data=data, headers=request_headers, method=method), timeout=15) as response:
                    _transport.observe(credential, kind, response.status, response.headers)
                    if coordinator:
                        coordinator.observe(credential, response.status, response.headers)
                    limit = (MAX_TREE_RESPONSE if "/git/trees/" in url else
                             MAX_SNAPSHOT_FILE * 2 if "/git/blobs/" in url else MAX_FILE * 2)
                    raw = response.read(limit + 1)
                    if len(raw) > limit:
                        raise BridgeError("upstream_response_too_large", 502)
                    # Invalid JSON is a failed request, and must never enter cache.
                    parsed = json.loads(raw)
                    if key is not None and (not isinstance(parsed, dict)
                                            or parsed.get("sha") != urlsplit(url).path.rsplit("/", 1)[-1]):
                        raise BridgeError("invalid_upstream_response", 502)
                    return raw
            except HTTPError as error:
                classified = github_http_error(error.code, error.headers, error.read(8192), method)
                _transport.observe(credential, kind, error.code, error.headers, classified)
                if coordinator:
                    coordinator.observe(credential, error.code, error.headers, classified)
                raise classified from None
            except (URLError, ValueError, TimeoutError):
                raise BridgeError("github_request_failed", 502) from None

    raw = await asyncio.to_thread(_transport.get_or_fetch, key, credential, fetch)
    # Each caller owns a fresh decoded object; cached bytes cannot be mutated.
    return json.loads(raw)


async def fetch_json(url, headers):
    return await send_json("GET", url, headers)


class BoundedReader:
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def read(self, size=-1):
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise BridgeError("archive_too_large", 413)
        return data


def read_archive(stream, entries, byte_limit=None):
    """Stream normal selected files only; never extract paths or Git metadata."""
    import gzip
    import tarfile
    from .core import MAX_ARCHIVE_BYTES, MAX_TREE_ENTRIES, safe_path

    byte_limit = MAX_ARCHIVE_BYTES if byte_limit is None else byte_limit
    files, prefix, count = {}, None, 0
    compressed = BoundedReader(stream, byte_limit)
    expanded = BoundedReader(gzip.GzipFile(fileobj=compressed), byte_limit)
    try:
        with tarfile.open(fileobj=expanded, mode="r|") as archive:
            for member in archive:
                count += 1
                if count > MAX_TREE_ENTRIES + 1:
                    raise BridgeError("too_many_snapshot_entries", 413)
                name = member.name.removesuffix("/")
                safe_path(name)
                top, _, path = name.partition("/")
                if prefix is None:
                    prefix = top
                if prefix != top:
                    raise BridgeError("invalid_upstream_response", 502)
                if path not in entries:
                    continue
                if not member.isfile() or member.size != entries[path]["size"] or path in files:
                    raise BridgeError("unsupported_repository_entry", 422)
                if not 0 <= member.size <= MAX_SNAPSHOT_FILE:
                    raise BridgeError("file_too_large", 413)
                content = archive.extractfile(member).read(member.size + 1)
                if len(content) != member.size:
                    raise BridgeError("invalid_upstream_response", 502)
                files[path] = content
    except (tarfile.TarError, OSError, EOFError, ValueError):
        raise BridgeError("invalid_upstream_response", 502) from None
    if set(files) != set(entries):
        raise BridgeError("invalid_upstream_response", 502)
    return files


async def fetch_archive(repository, commit, headers, entries, *, byte_limit=None):
    """Download a commit/tree object resolved by the core, with bounded extraction."""
    from urllib.parse import quote, urlsplit
    coordinator = coordinator_from_env()

    def download():
        credential = credential_key(headers)
        url = "https://api.github.com/repos/" + quote(repository, safe="/") + "/tarball/" + commit
        opener = build_opener(NoRedirects())
        with _transport.upstream(credential), (coordinator.upstream(credential) if coordinator else nullcontext()):
            try:
                try:
                    _transport.started(credential)
                    response = opener.open(Request(url, headers=headers), timeout=30)
                    _transport.observe(credential, "archive", response.status, response.headers)
                    if coordinator:
                        coordinator.observe(credential, response.status, response.headers)
                except HTTPError as redirect:
                    if redirect.code != 302:
                        raise
                    _transport.observe(credential, "archive", redirect.code, redirect.headers)
                    if coordinator:
                        coordinator.observe(credential, redirect.code, redirect.headers)
                    target = redirect.headers.get("Location", "")
                    redirect.close()
                    parts = urlsplit(target)
                    if (parts.scheme != "https" or parts.netloc != "codeload.github.com"
                            or parts.username or parts.password):
                        raise BridgeError("invalid_upstream_response", 502)
                    # The signed download URL supplies its own authorization.
                    # This continuation consumes no further GitHub API request.
                    _transport.started(credential)
                    response = opener.open(Request(target, headers={"User-Agent": headers["User-Agent"]}), timeout=30)
                    _transport.observe(credential, "archive", response.status, response.headers)
                with response:
                    return read_archive(response, entries, byte_limit)
            except HTTPError as error:
                classified = github_http_error(error.code, error.headers, error.read(8192))
                _transport.observe(credential, "archive", error.code, error.headers, classified)
                if coordinator:
                    coordinator.observe(credential, error.code, error.headers, classified)
                raise classified from None
            except (URLError, TimeoutError):
                raise BridgeError("github_request_failed", 502) from None
    return await asyncio.to_thread(download)


async def fetch_query_archive(repository, commit, headers, entries):
    return await fetch_archive(repository, commit, headers, entries, byte_limit=64 * 1024 * 1024)
