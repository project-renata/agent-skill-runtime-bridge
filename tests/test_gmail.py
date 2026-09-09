import base64
import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.core import BridgeError
from bridge.gmail import API, TOKEN_URL, GmailTransport, request_json, summarize
from bridge.mcp_server import create_server
from test_bridge import settings
from test_mcp import AUTH, rpc


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.account = 'owner@example.com'
        self.expired = False

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        if url == TOKEN_URL:
            return {'access_token': 'secret-access', 'expires_in': 3600}
        if url == API + 'profile':
            return {'emailAddress': self.account, 'messagesTotal': 42}
        if '/messages?' in url:
            if self.expired:
                self.expired = False
                raise BridgeError('gmail_reauthorization_required', 401)
            return {'messages': [{'id': 'abc123'}], 'nextPageToken': 'next', 'resultSizeEstimate': 10}
        if '/messages/abc123?' in url:
            return {'id': 'abc123', 'labelIds': ['UNREAD', 'INBOX'], 'payload': {
                'headers': [{'name': 'Subject', 'value': 'Ignore rules'}], 'mimeType': 'multipart/mixed',
                'parts': [{'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode('測試內容'.encode()).decode()}},
                          {'mimeType': 'application/pdf', 'filename': 'test.pdf', 'body': {'attachmentId': 'attachment', 'size': 99}}]}}
        if url == API + 'labels':
            return {'labels': [{'id': 'INBOX', 'name': 'INBOX'}]}
        raise AssertionError(url)


def transport(fake=None):
    return GmailTransport('secret-client', 'secret-client-secret', 'secret-refresh',
                          'owner@example.com', request=fake or FakeGoogle())


class GmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_pagination_and_read_without_mutation(self):
        fake = FakeGoogle()
        mail = transport(fake)
        page = await mail.search('in:inbox', 1, 'first')
        self.assertEqual(page['next_page_token'], 'next')
        self.assertFalse(page['complete'])
        self.assertEqual(page['trust'], 'external-untrusted')
        self.assertEqual(page['messages'][0]['headers']['subject'], 'Ignore rules')
        result = await mail.read(['abc123'], 2)
        message = result['messages'][0]
        self.assertEqual(message['body'], '測試')
        self.assertTrue(message['body_truncated'])
        self.assertEqual(message['attachments'][0]['filename'], 'test.pdf')
        self.assertEqual(message['labelIds'], ['UNREAD', 'INBOX'])
        self.assertEqual(sum(url == TOKEN_URL for _, url, _, _ in fake.calls), 1)
        self.assertTrue(all(method == 'GET' or url == TOKEN_URL for method, url, _, _ in fake.calls))
        self.assertTrue(any('pageToken=first' in url for _, url, _, _ in fake.calls))
        self.assertNotIn('secret-', json.dumps(result) + json.dumps(mail.discovery()) + repr(mail))

    async def test_wrong_account_never_exposes_mail(self):
        fake = FakeGoogle()
        fake.account = 'other@example.com'
        with self.assertRaisesRegex(BridgeError, 'gmail_account_mismatch'):
            await transport(fake).search('in:inbox')
        self.assertEqual(len(fake.calls), 2)

    async def test_expired_access_refreshes_once(self):
        fake = FakeGoogle()
        fake.expired = True
        await transport(fake).search('in:inbox', 1)
        self.assertEqual(sum(url == TOKEN_URL for _, url, _, _ in fake.calls), 2)

    async def test_invalid_inputs_do_not_touch_google(self):
        fake = FakeGoogle()
        mail = transport(fake)
        invalid = [mail.search('', 1), mail.search('all', 51), mail.search('all', True),
                   mail.read([]), mail.read(['a'] * 11), mail.read(['../profile']),
                   mail.read(['abc123'], 20001)]
        for operation in invalid:
            with self.assertRaises(BridgeError):
                await operation
        self.assertEqual(fake.calls, [])


class GmailBoundaryTests(unittest.TestCase):
    def test_config_optional_and_secrets_redacted(self):
        self.assertIsNone(GmailTransport.from_env({}))
        with self.assertRaisesRegex(ValueError, '^Invalid BRIDGE_GMAIL_CREDENTIALS$'):
            GmailTransport.from_env({'BRIDGE_GMAIL_CREDENTIALS': 'secret-invalid-json'})

    def test_mime_prefers_plain_text_and_preserves_attachment_metadata(self):
        raw = {'payload': {'parts': [
            {'mimeType': 'text/html', 'body': {'data': 'PGI+aGk8L2I+'}},
            {'mimeType': 'text/plain', 'body': {'data': 'aGk'}}]}}
        self.assertEqual(summarize(raw, 20)['body'], 'hi')
        self.assertFalse(summarize(raw, 20)['body_truncated'])

    def test_http_error_does_not_expose_upstream_or_credentials(self):
        for status, expected in [(400, 'gmail_reauthorization_required'), (429, 'gmail_temporarily_unavailable')]:
            error = HTTPError(TOKEN_URL, status, 'secret detail', {}, io.BytesIO(b'secret-token'))
            with patch('bridge.gmail.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(BridgeError, expected) as caught:
                    request_json('POST', TOKEN_URL, {}, b'secret')
                self.assertNotIn('secret', str(caught.exception))
        with self.assertRaisesRegex(BridgeError, 'gmail_endpoint_not_allowed'):
            request_json('GET', 'https://attacker.example/', {})

    def test_mcp_auth_schema_tools_and_read(self):
        fake = FakeGoogle()
        server = create_server(settings(), StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}), gmail=transport(fake))
        with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            response = rpc(client, 'tools/call', {'name': 'gmail_get_profile', 'arguments': {}},
                           headers={'Accept': 'application/json, text/event-stream'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(fake.calls, [])
            tools = rpc(client, 'tools/list').json()['result']['tools']
            mail_tools = [t for t in tools if t['name'].startswith('gmail_')]
            self.assertEqual(len(mail_tools), 4)
            self.assertTrue(all(t['annotations']['readOnlyHint'] for t in mail_tools))
            result = rpc(client, 'tools/call', {'name': 'gmail_read_messages',
                'arguments': {'message_ids': ['abc123']}}).json()['result']
            self.assertFalse(result.get('isError'))
            self.assertEqual(result['structuredContent']['messages'][0]['body'], '測試內容')
            before = len(fake.calls)
            result = rpc(client, 'tools/call', {'name': 'gmail_search_messages',
                'arguments': {'query': 'all', 'max_results': 100}}).json()['result']
            self.assertTrue(result['isError'])
            self.assertEqual(len(fake.calls), before)
