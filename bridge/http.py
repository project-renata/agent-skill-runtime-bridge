"""CPython's GitHub transport. Credentials only reach api.github.com."""
import asyncio
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .core import BridgeError, MAX_FILE, MAX_TREE_RESPONSE


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def send_json(method, url, headers, body=None):
    def fetch():
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
            request_headers = {**headers, "Content-Type": "application/json"} if data is not None else headers
            with build_opener(NoRedirects).open(Request(url, data=data, headers=request_headers, method=method), timeout=15) as response:
                limit = MAX_TREE_RESPONSE if "/git/trees/" in url else MAX_FILE * 2
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise BridgeError("upstream_response_too_large", 502)
                return json.loads(raw)
        except HTTPError as error:
            if method == "PATCH" and error.code in (409, 422):
                raise BridgeError("branch_conflict", 409) from None
            code = "repository_entry_not_found" if error.code == 404 else "github_request_failed"
            raise BridgeError(code, 502) from None
        except (URLError, ValueError, TimeoutError):
            raise BridgeError("github_request_failed", 502) from None
    return await asyncio.to_thread(fetch)


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


def read_archive(stream, entries):
    """Stream normal selected files only; never extract paths or Git metadata."""
    import gzip
    import tarfile
    from .core import MAX_SNAPSHOT_TOTAL, MAX_TREE_ENTRIES, safe_path

    files, prefix, count = {}, None, 0
    compressed = BoundedReader(stream, MAX_SNAPSHOT_TOTAL * 2)
    expanded = BoundedReader(gzip.GzipFile(fileobj=compressed), MAX_SNAPSHOT_TOTAL * 2)
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
                content = archive.extractfile(member).read(MAX_FILE + 1)
                if len(content) != member.size:
                    raise BridgeError("invalid_upstream_response", 502)
                files[path] = content
    except (tarfile.TarError, OSError, EOFError, ValueError):
        raise BridgeError("invalid_upstream_response", 502) from None
    if set(files) != set(entries):
        raise BridgeError("invalid_upstream_response", 502)
    return files


async def fetch_archive(repository, commit, headers, entries):
    from urllib.parse import quote, urlsplit

    def download():
        url = "https://api.github.com/repos/" + quote(repository, safe="/") + "/tarball/" + commit
        opener = build_opener(NoRedirects())
        try:
            try:
                response = opener.open(Request(url, headers=headers), timeout=30)
            except HTTPError as redirect:
                if redirect.code != 302:
                    raise
                target = redirect.headers.get("Location", "")
                parts = urlsplit(target)
                if (parts.scheme != "https" or parts.netloc != "codeload.github.com"
                        or parts.username or parts.password):
                    raise BridgeError("invalid_upstream_response", 502)
                # The signed GitHub download URL supplies its own authorization.
                # Do not forward the repository token to a redirect destination.
                response = opener.open(Request(target, headers={"User-Agent": headers["User-Agent"]}), timeout=30)
            with response:
                return read_archive(response, entries)
        except (HTTPError, URLError, TimeoutError):
            raise BridgeError("github_request_failed", 502) from None
    return await asyncio.to_thread(download)
