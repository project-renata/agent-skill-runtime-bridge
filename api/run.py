import asyncio
from http.server import BaseHTTPRequestHandler
import json
import os

from bridge.core import BridgeError, MAX_REQUEST, Settings, handle
from bridge.execution import execute_subprocess
from bridge.http import fetch_json


class handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, {"service": "agent-skill-runtime-bridge", "protocol": "0.1", "mode": "read-only"})

    def do_POST(self):
        try:
            settings = Settings.from_env(os.environ)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise BridgeError("unsupported_media_type", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_REQUEST:
                raise BridgeError("request_too_large", 413)
            raw = self.rfile.read(length)
            status, payload = asyncio.run(handle(raw, self.headers.get("Authorization", ""),
                                                 settings, fetch_json, execute_subprocess))
            self.reply(status, payload)
        except BridgeError as error:
            self.reply(error.status, {"ok": False, "error": {"code": error.code}})
        except (ValueError, OSError):
            self.reply(400, {"ok": False, "error": {"code": "invalid_request"}})

    def log_message(self, *args):
        pass
