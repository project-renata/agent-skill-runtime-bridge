"""CPython's GitHub transport. Credentials only reach api.github.com."""
import asyncio
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .core import BridgeError, MAX_FILE


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def fetch_json(url, headers):
    def fetch():
        try:
            with build_opener(NoRedirects).open(Request(url, headers=headers), timeout=15) as response:
                raw = response.read(MAX_FILE * 2 + 1)
                if len(raw) > MAX_FILE * 2:
                    raise BridgeError("upstream_response_too_large", 502)
                return json.loads(raw)
        except HTTPError as error:
            code = "repository_entry_not_found" if error.code == 404 else "github_request_failed"
            raise BridgeError(code, 502) from None
        except (URLError, ValueError, TimeoutError):
            raise BridgeError("github_request_failed", 502) from None
    return await asyncio.to_thread(fetch)
