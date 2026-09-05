"""CPython's GitHub transport. Credentials only reach api.github.com."""
import asyncio
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .core import BridgeError, MAX_FILE


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def send_json(method, url, headers, body=None):
    def fetch():
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
            request_headers = {**headers, "Content-Type": "application/json"} if data is not None else headers
            with build_opener(NoRedirects).open(Request(url, data=data, headers=request_headers, method=method), timeout=15) as response:
                raw = response.read(MAX_FILE * 2 + 1)
                if len(raw) > MAX_FILE * 2:
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
