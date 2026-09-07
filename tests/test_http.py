"""Upstream throttling is distinct from authorization; never expose raw errors."""
import asyncio
import io
import unittest
from urllib.error import HTTPError
from unittest.mock import patch, MagicMock

from bridge.core import BridgeError, github_http_error, handle
from bridge.http import send_json, fetch_archive
from test_bridge import KEY, settings, request


class HTTPErrorTests(unittest.TestCase):
    def test_primary_secondary_and_real_permission_denials(self):
        for status, headers, body, expected in [
            (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1788744866'}, b'', 'github_rate_limited'),
            (403, {'Retry-After': '60'}, b'', 'github_rate_limited'),
            (403, {}, b'{"message":"You have exceeded a secondary rate limit."}', 'github_rate_limited'),
            (429, {}, b'', 'github_rate_limited'),
            (403, {}, b'{"message":"Resource not accessible by personal access token"}', 'github_forbidden'),
            (401, {}, b'', 'github_unauthorized'),
            (404, {}, b'', 'repository_entry_not_found'),
        ]:
            with self.subTest(status=status, headers=headers):
                error = github_http_error(status, headers, body)
                self.assertEqual(error.code, expected)
                self.assertEqual(error.status, 429 if expected == 'github_rate_limited' else 502)
        self.assertEqual(github_http_error(422, {}, method='PATCH').code, 'branch_conflict')

    def test_transport_keeps_only_safe_numeric_retry_metadata(self):
        headers = {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1788744866', 'Retry-After': 'secret'}
        error = HTTPError('https://api.github.com/repos/private', 403, 'Forbidden', headers,
                          io.BytesIO(b'{"message":"private response text"}'))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch('bridge.http.build_opener', return_value=opener), self.assertRaises(BridgeError) as ctx:
            asyncio.run(send_json('GET', 'https://api.github.com/repos/private', {'Authorization': 'secret'}))
        self.assertEqual(ctx.exception.details, {'upstream_status': 403, 'reset_at': 1788744866})
        self.assertNotIn('secret', str(ctx.exception))

    def test_archive_rate_limit_uses_same_classifier(self):
        error = HTTPError('https://api.github.com/repos/private/tarball/x', 403, 'Forbidden',
                          {'X-RateLimit-Remaining': '0'}, io.BytesIO(b'{}'))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch('bridge.http.build_opener', return_value=opener), self.assertRaisesRegex(BridgeError, 'github_rate_limited'):
            asyncio.run(fetch_archive('owner/private', 'a' * 40, {'User-Agent': 'bridge'}, {}))

    def test_rate_limit_stops_before_execute_or_write_and_reports_reset(self):
        called = []
        async def fetch(*args):
            raise github_http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1788744866'})
        status, body = asyncio.run(handle(request(), 'Bearer ' + KEY, settings(), fetch,
                                         lambda *args: called.append('execute')))
        self.assertEqual(status, 429)
        self.assertEqual(body['error']['reset_at'], 1788744866)
        self.assertEqual(called, [])
