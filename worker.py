"""Cloudflare transport only; protocol and program stay provider-neutral."""
import json
from workers import WorkerEntrypoint, Response, fetch

from bridge.core import BridgeError, MAX_FILE, MAX_REQUEST, Settings, handle
from bridge.execution import execute_inline


async def fetch_json(url, headers):
    response = await fetch(url, headers=headers, redirect="manual")
    if response.status != 200:
        raise BridgeError("github_request_failed", 502)
    raw = await response.text()
    if len(raw.encode()) > MAX_FILE * 2:
        raise BridgeError("upstream_response_too_large", 502)
    return json.loads(raw)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        if request.method == "GET":
            return Response(json.dumps({"service": "agent-skill-runtime-bridge", "protocol": "0.1", "mode": "read-only"}), headers=headers)
        if request.method != "POST":
            return Response(json.dumps({"ok": False, "error": {"code": "method_not_allowed"}}), status=405, headers=headers)
        try:
            settings = Settings.from_env({key: getattr(self.env, key, "") for key in
                                         ("BRIDGE_API_KEY", "BRIDGE_REPOSITORIES", "BRIDGE_GITHUB_TOKEN")})
            if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise BridgeError("unsupported_media_type", 415)
            length = request.headers.get("Content-Length")
            if length and int(length) > MAX_REQUEST:
                raise BridgeError("request_too_large", 413)
            raw = (await request.text()).encode()
            status, body = await handle(raw, request.headers.get("Authorization", ""), settings, fetch_json, execute_inline)
        except BridgeError as error:
            status, body = error.status, {"ok": False, "error": {"code": error.code}}
        except Exception:
            status, body = 500, {"ok": False, "error": {"code": "execution_failed"}}
        return Response(json.dumps(body, ensure_ascii=False), status=status, headers=headers)
