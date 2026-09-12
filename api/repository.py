import asyncio
from http.server import BaseHTTPRequestHandler
import json
import os

from bridge.core import BridgeError
from bridge.repository import MAX_INPUT
from bridge.repository_http import handle_repository, service_from_env


class handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        try:
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise BridgeError('unsupported_media_type', 415)
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= MAX_INPUT:
                raise BridgeError('repository_request_too_large', 413)
            service = service_from_env(os.environ)
            status, result = asyncio.run(handle_repository(self.rfile.read(length),
                self.headers.get('Authorization', ''), service))
            self.reply(status, result)
        except BridgeError as error:
            self.reply(error.status, {'ok': False, 'error': {'code': error.code}})
        except (ValueError, OSError):
            self.reply(400, {'ok': False, 'error': {'code': 'invalid_repository_request'}})

    def do_GET(self):
        self.reply(405, {'ok': False, 'error': {'code': 'post_required'}})

    def log_message(self, *args):
        pass
