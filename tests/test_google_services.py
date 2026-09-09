import asyncio
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.core import BridgeError
from bridge.google_services import GoogleServices, GoogleCatalog, google_http, resource_fingerprint
from bridge.google_journal import MemoryGoogleJournal, LocalGoogleJournal, RedisGoogleJournal
from bridge.google_documents import extract_document, read_document
from bridge.mcp_server import create_server
from test_bridge import settings
from test_mcp import AUTH, rpc


class Mail:
    account = 'owner@example.com'
    _lock = threading.RLock()
    _access, _deadline = '', 0

    def _token(self):
        return 'never-expose-access-token'


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.label = {'id': 'Label_1', 'name': 'before', 'etag': 'version1'}
        self.fail_write = False
        self.fail_readback = False
        self.sent = 0

    def __call__(self, method, url, headers, body=None, binary=False):
        self.calls.append((method, url, deepcopy(headers), body))
        if method == 'GET':
            if self.fail_readback and self.sent:
                raise BridgeError('google_effect_unknown', 502)
            if 'labels/Label_1' in url:
                if self.label is None:
                    raise BridgeError('google_not_found', 404)
                return deepcopy(self.label)
            if '/messages/' in url:
                return {'id': 'abc123', 'threadId': 'abc', 'payload': {'mimeType': 'text/plain',
                    'headers': [{'name': 'Subject', 'value': 'Topic'}, {'name': 'Message-ID', 'value': '<source@example.com>'}],
                    'body': {'data': base64.urlsafe_b64encode(b'hello').decode()}}}
            return {'items': [], 'nextPageToken': 'more'}
        if self.fail_write:
            self.sent += 1
            raise BridgeError('google_effect_unknown', 502)
        self.sent += 1
        if method == 'DELETE':
            self.label = None
            return {}
        if 'labels/Label_1' in url:
            self.label.update(json.loads(body), etag='version2')
            return deepcopy(self.label)
        return {'id': 'sent123'}


class GoogleServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake, self.journal = FakeAPI(), MemoryGoogleJournal()
        self.google = GoogleServices(Mail(), self.journal, request=self.fake)

    async def test_prepare_no_effect_execute_readback_and_retry(self):
        preview = await self.google.prepare([{'operation': 'gmail.users.labels.patch',
            'params': {'id': 'Label_1'}, 'body': {'name': 'after'}}], 'rename-label-once')
        self.assertEqual(preview['precondition_count'], 1)
        self.assertEqual(self.fake.sent, 0)
        result = await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['effects'][0]['status'], 'read_back')
        self.assertEqual(result['effects'][0]['readback']['data']['name'], 'after')
        self.assertEqual(next(x[2]['If-Match'] for x in self.fake.calls if x[0] == 'PATCH'), 'version1')
        self.assertEqual(await self.google.execute(preview['plan_id'], preview['plan_hash']), result)
        self.assertEqual(self.fake.sent, 1)
        self.assertNotIn('never-expose', json.dumps(result) + json.dumps(preview))

    async def test_changed_source_blocks_effect(self):
        preview = await self.google.prepare([{'operation': 'gmail.users.labels.delete',
                                             'params': {'id': 'Label_1'}}], 'delete-label-once')
        self.fake.label['name'] = 'changed elsewhere'
        result = await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(self.fake.sent, 0)

    async def test_failed_expected_state_retains_later_tracking_task(self):
        changes = [{'operation': 'gmail.users.labels.patch', 'params': {'id': 'Label_1'},
                    'body': {'name': 'after'}, 'verify': {'require_readback': True, 'expected': {'name': 'unexpected'}}},
                   {'operation': 'tasks.tasks.delete', 'params': {'tasklist': 'list', 'task': 'tracker'}}]
        preview = await self.google.prepare(changes, 'retain-tracker-on-unverified-first-effect')
        result = await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(result['status'], 'applied_verification_failed')
        self.assertEqual(self.fake.sent, 1)
        self.assertFalse(any(c[0] == 'DELETE' for c in self.fake.calls))

    async def test_delete_verifies_absence(self):
        preview = await self.google.prepare([{'operation': 'gmail.users.labels.delete',
                                             'params': {'id': 'Label_1'}}], 'delete-label-once')
        result = await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(result['effects'][0]['status'], 'absence_verified')

    async def test_unknown_effect_is_never_retried_and_later_effect_stops(self):
        self.fake.fail_write = True
        changes = [{'operation': 'gmail.users.messages.send', 'body': {'raw': 'VG86IHRlc3RAZXhhbXBsZS5jb20NCg0KdGVzdA=='}}] * 2
        preview = await self.google.prepare(changes, 'send-exactly-once')
        a, b = await asyncio.gather(*[self.google.execute(preview['plan_id'], preview['plan_hash']) for _ in range(2)])
        self.assertIn(a['status'], ('effect_unknown', 'pending_or_effect_unknown'))
        self.assertIn(b['status'], ('effect_unknown', 'pending_or_effect_unknown'))
        self.assertEqual(self.fake.sent, 1)
        await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(self.fake.sent, 1)

    async def test_readback_failure_not_reported_as_write_failure(self):
        self.fake.fail_readback = True
        preview = await self.google.prepare([{'operation': 'gmail.users.labels.patch',
            'params': {'id': 'Label_1'}, 'body': {'name': 'after'}}], 'readback-failure')
        result = await self.google.execute(preview['plan_id'], preview['plan_hash'])
        self.assertEqual(result['status'], 'applied_verification_failed')
        self.assertEqual(result['completed_changes'], 1)
        self.assertEqual(self.fake.sent, 1)

    async def test_idempotency_conflict_plan_hash_expiry_and_claim_survives(self):
        call = {'operation': 'tasks.tasklists.insert', 'body': {'title': 'one'}}
        preview = await self.google.prepare([call], 'unique-task-list')
        self.assertEqual(await self.google.prepare([call], 'unique-task-list'), preview)
        with self.assertRaisesRegex(BridgeError, 'google_idempotency_conflict'):
            await self.google.prepare([{**call, 'body': {'title': 'two'}}], 'unique-task-list')
        with self.assertRaisesRegex(BridgeError, 'google_plan_missing_or_mismatch'):
            await self.google.execute(preview['plan_id'], '0' * 64)
        self.journal.claim(preview['plan_id'])
        self.assertEqual((await self.google.execute(preview['plan_id'], preview['plan_hash']))['status'], 'pending_or_effect_unknown')
        self.assertEqual(self.fake.sent, 0)
        plan = self.journal.get('plan:' + preview['plan_id'])
        plan['expires_at'] = 0
        self.journal.put('plan:' + preview['plan_id'], plan)
        with self.assertRaisesRegex(BridgeError, 'google_plan_expired'):
            await self.google.execute(preview['plan_id'], preview['plan_hash'])

    async def test_pagination_account_lock_and_no_url_injection(self):
        result = await self.google.read('calendar.events.list', {'calendarId': 'owner@example.com', 'maxResults': 2})
        self.assertFalse(result['complete'])
        self.assertEqual(result['next_page_token'], 'more')
        for op, params in [('gmail.users.messages.list', {'userId': 'other@example.com'}),
                           ('drive.files.list', {'pageSize': 101}),
                           ('drive.files.list', {'access_token': 'secret'}),
                           ('drive.files.get', {'fileId': '..'}),
                           ('tasks.tasks.delete', {'tasklist': 'one', 'task': 'two'}),
                           ('drive.files.list', {'uploadType': 'media'})]:
            before = len(self.fake.calls)
            with self.assertRaises(BridgeError):
                await self.google.read(op, params)
            self.assertEqual(len(self.fake.calls), before)

    async def test_native_body_types_and_nested_schema(self):
        self.google.catalog.normalize({'operation': 'sheets.spreadsheets.values.update',
            'params': {'spreadsheetId': 'sheet', 'range': 'A1:B2', 'valueInputOption': 'USER_ENTERED'},
            'body': {'values': [['中文', True], [1, '=A2+1']]}}, Mail.account)
        valid = {'operation': 'docs.documents.batchUpdate', 'params': {'documentId': 'doc'},
                 'body': {'requests': [{'insertText': {'endOfSegmentLocation': {}, 'text': '你好'}}]}}
        self.google.catalog.normalize(valid, Mail.account)
        for body in [{'requests': [{'inventedOperation': {}}]}, {'requests': 'oops'}, {'unknown': 'field'}]:
            with self.assertRaises(BridgeError):
                self.google.catalog.normalize({**valid, 'body': body}, Mail.account)

    async def test_tasks_put_binds_body_id_to_path(self):
        for operation, params, expected in [('tasks.tasklists.update', {'tasklist': 'list'}, 'list'),
                                             ('tasks.tasks.update', {'tasklist': 'list', 'task': 'task'}, 'task')]:
            call = self.google.catalog.normalize({'operation': operation, 'params': params, 'body': {'title': 'renamed'}}, Mail.account)
            self.assertEqual(call['body']['id'], expected)
            with self.assertRaisesRegex(BridgeError, 'google_body_target_mismatch'):
                self.google.catalog.normalize({'operation': operation, 'params': params, 'body': {'id': 'other', 'title': 'renamed'}}, Mail.account)

    async def test_media_upload_payload_preview_and_fixed_url(self):
        call = {'operation': 'drive.files.create', 'body': {'name': '你好.txt'},
                'media': {'mime_type': 'text/plain', 'data_base64': base64.b64encode(b'hello').decode()}}
        preview = await self.google.prepare([call], 'upload-scratch-one')
        self.assertNotIn('data_base64', preview['changes'][0]['media'])
        self.assertEqual(preview['changes'][0]['media']['size_bytes'], 5)
        await self.google.execute(preview['plan_id'], preview['plan_hash'])
        upload = next(c for c in self.fake.calls if c[0] == 'POST')
        self.assertEqual(upload[1], 'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart')
        self.assertIn(b'hello', upload[3])
        self.assertIn('multipart/related', upload[2]['Content-Type'])

    async def test_compose_reply_forward_and_attachment_without_send(self):
        result = self.google.compose(['recipient@example.com'], 'Re: Topic', '你好',
            reply_message='abc123', attachments=[{'filename': 'test.txt', 'mime_type': 'text/plain', 'data_base64': 'aGk='}])
        self.assertEqual(result['message']['threadId'], 'abc')
        preview = result['preview']['body']['mail_preview']
        self.assertEqual(preview['headers']['In-Reply-To'], '<source@example.com>')
        self.assertEqual(preview['attachments'][0]['size_bytes'], 2)
        self.assertEqual(self.fake.sent, 0)
        forward = self.google.compose(['recipient@example.com'], 'Fwd: Topic', 'For you', forward_message='abc123')
        self.assertIn('hello', forward['preview']['body']['mail_preview']['body'])
        with self.assertRaises(BridgeError):
            self.google.compose(['a\r\nBcc: bad@example.com'], 'hi', 'hi')

    async def test_explicit_source_check_bound_to_plan(self):
        check = {'operation': 'gmail.users.messages.get', 'params': {'id': 'abc123'}}
        observed = self.google._read(check)
        call = {'operation': 'tasks.tasklists.insert', 'body': {'title': 'source-bound'},
                'checks': [{'call': check, 'fingerprint': observed['fingerprint']}]}
        preview = await self.google.prepare([call], 'source-bound-create')
        self.assertEqual(preview['precondition_count'], 1)
        call['checks'][0]['fingerprint'] = '0' * 64
        with self.assertRaisesRegex(BridgeError, 'google_stale_source'):
            await self.google.prepare([call], 'source-bound-wrong')


class GoogleBoundaryTests(unittest.TestCase):
    def test_rotating_gmail_attachment_handles_do_not_conflict_but_content_edits_do(self):
        a = {'id': 'draft1', 'message': {'id': 'msg1', 'payload': {'parts': [{'partId': '1', 'filename': 'a.pdf', 'body': {'size': 99, 'attachmentId': 'old'}}]}}}
        b = deepcopy(a)
        b['message']['payload']['parts'][0]['body']['attachmentId'] = 'rotated'
        self.assertEqual(resource_fingerprint('gmail.users.drafts.get', a), resource_fingerprint('gmail.users.drafts.get', b))
        b['message']['id'] = 'msg2'
        self.assertNotEqual(resource_fingerprint('gmail.users.drafts.get', a), resource_fingerprint('gmail.users.drafts.get', b))
        self.assertEqual(resource_fingerprint('drive.files.get', {'id': 'file', 'thumbnailLink': 'old'}),
                         resource_fingerprint('drive.files.get', {'id': 'file', 'thumbnailLink': 'new'}))

    def test_registration_confirmation_order_duplicate_and_missing_evidence(self):
        from bridge.google_workflows import prepare_workflow
        from bridge.google_services import fingerprint
        google = GoogleServices(Mail(), MemoryGoogleJournal(), request=FakeAPI())
        marker = '[renata-followup:' + fingerprint({'account': Mail.account, 'key': 'registration-one'})[:40] + ']'
        task = {'id': 'tracker', 'title': 'Await confirmation', 'notes': marker}
        original = google._read
        def read(call):
            operation = call['operation']
            if operation == 'tasks.tasks.list':
                data = {'items': [task]}
            elif operation == 'tasks.tasks.get':
                data = task
            elif operation == 'calendar.events.get':
                raise BridgeError('google_not_found', 404)
            else:
                return original(call)
            return {'data': data, 'fingerprint': fingerprint(data), 'next_page_token': None}
        google._read = read
        args = {'tracking_key': 'registration-one', 'message_id': 'abc123', 'tasklist_id': 'list',
                'calendar_id': 'primary', 'confirmation_quote': 'hello',
                'event': {'summary': 'Confirmed event', 'start': {'date': '2026-09-11'}, 'end': {'date': '2026-09-12'}}}
        result = prepare_workflow(google, 'registration_confirm', args, 'registration-confirm-once')
        self.assertEqual([c['operation'] for c in result['changes']], ['calendar.events.insert', 'tasks.tasks.delete'])
        self.assertTrue(result['changes'][0]['verify']['require_readback'])
        self.assertEqual(result['changes'][0]['verify']['expected']['status'], 'confirmed')
        self.assertEqual(prepare_workflow(google, 'registration_confirm', args, 'registration-confirm-once'), result)
        with self.assertRaisesRegex(BridgeError, 'google_confirmation_source_quote_required'):
            prepare_workflow(google, 'registration_confirm', {**args, 'confirmation_quote': 'not in source'}, 'registration-invalid-source')

    def test_document_stable_part_selector_refreshes_attachment_handle(self):
        from bridge.google_services import fingerprint
        class Source:
            account = Mail.account
            def _read(self, call):
                if call['operation'] == 'gmail.users.messages.get':
                    data = {'payload': {'parts': [{'partId': '1', 'mimeType': 'text/plain', 'body': {'attachmentId': 'fresh-handle'}}]}}
                else:
                    assert call['params']['id'] == 'fresh-handle'
                    data = {'data': base64.urlsafe_b64encode(b'readable attachment').decode()}
                return {'data': data, 'fingerprint': fingerprint(data)}
        result = read_document(Source(), {'message_id': 'abc123', 'part_id': '1'})
        self.assertEqual(result['text'], 'readable attachment')

    def test_catalog_all_methods_have_pinned_https_and_read_write_classification(self):
        catalog = GoogleCatalog()
        self.assertEqual(len(catalog.methods), 220)
        self.assertEqual(set(catalog.documents), {'gmail', 'calendar', 'tasks', 'drive', 'docs', 'sheets', 'slides'})
        for name, spec in catalog.methods.items():
            self.assertTrue(spec['base_url'].startswith('https://'))
            self.assertNotIn('..', spec['path'])
            if spec['httpMethod'] in ('DELETE', 'PUT', 'PATCH'):
                self.assertFalse(spec['readonly'], name)
        self.assertTrue(catalog.method('calendar.freebusy.query')['readonly'])
        self.assertFalse(catalog.method('sheets.spreadsheets.values.batchClear')['readonly'])
        self.assertEqual(catalog.describe(operation='docs.documents.batchUpdate')['request_schema'], 'BatchUpdateDocumentRequest')
        self.assertIn('requests', catalog.describe(service='docs', schema='BatchUpdateDocumentRequest')['definition']['properties'])

    def test_transport_empty_delete_binary_limits_and_sanitized_errors(self):
        with patch('bridge.google_services.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b''
            response.headers = {'Content-Type': 'text/plain'}
            self.assertEqual(google_http('DELETE', 'https://www.googleapis.com/drive/v3/files/x', {}), {})
            response.read.return_value = b'hello'
            binary = google_http('GET', 'https://www.googleapis.com/drive/v3/files/x?alt=media', {}, binary=True)
            self.assertEqual(base64.b64decode(binary['data_base64']), b'hello')
            error = HTTPError('url', 403, 'secret', {}, io.BytesIO(json.dumps({'error': {'message': 'secret',
                'errors': [{'reason': 'accessNotConfigured'}]}}).encode()))
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(BridgeError, 'google_api_not_enabled') as caught:
                google_http('GET', 'https://docs.googleapis.com/v1/documents/x', {})
            self.assertNotIn('secret', str(caught.exception))
        for url in ['http://www.googleapis.com/drive/v3/files', 'https://www.googleapis.com.evil.test/', 'https://evil.test/']:
            with self.assertRaisesRegex(BridgeError, 'google_endpoint_not_allowed'):
                google_http('GET', url, {})

    def test_local_journal_survives_process_and_encrypts_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            a = LocalGoogleJournal(directory)
            self.assertTrue(a.put('plan', {'private': 'mail-body-secret'}, once=True))
            self.assertFalse(a.put('plan', {}, once=True))
            self.assertTrue(a.claim('once'))
            b = LocalGoogleJournal(directory)
            self.assertEqual(b.get('plan'), {'private': 'mail-body-secret'})
            self.assertFalse(b.claim('once'))
            self.assertNotIn(b'mail-body-secret', Path(directory, 'journal.sqlite3').read_bytes())
            self.assertEqual(Path(directory, 'journal.key').stat().st_mode & 0o777, 0o600)

    def test_redis_encrypts_and_claim_has_no_expiry(self):
        from cryptography.fernet import Fernet
        with patch('bridge.google_journal.Redis') as redis:
            journal = RedisGoogleJournal('rediss://example.invalid', Fernet.generate_key())
            journal.put('plan', {'private': 'mail-body-secret'})
            args, kwargs = redis.from_url.return_value.set.call_args
            self.assertNotIn(b'mail-body-secret', args[1])
            self.assertEqual(kwargs['ex'], 86400)
            journal.claim('once')
            self.assertEqual(redis.from_url.return_value.set.call_args.kwargs, {'nx': True})

    def test_document_truncation_encrypted_pdf_and_source_binding(self):
        text = extract_document('測試'.encode(), 'text/plain', max_chars=1)
        self.assertEqual(text['text'], '測')
        self.assertTrue(text['truncated'])
        from pypdf import PdfWriter
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.encrypt('private-password')
        stream = io.BytesIO()
        pdf.write(stream)
        with self.assertRaisesRegex(BridgeError, 'password_reference_required_or_invalid'):
            extract_document(stream.getvalue(), 'application/pdf')
        result = extract_document(stream.getvalue(), 'application/pdf', password='private-password')
        self.assertTrue(result['needs_local_ocr'])
        self.assertNotIn('private-password', json.dumps(result))
        google = GoogleServices(Mail(), MemoryGoogleJournal(), request=FakeAPI())
        with self.assertRaisesRegex(BridgeError, 'google_attachment_not_in_message'):
            read_document(google, {'message_id': 'abc123', 'attachment_id': 'not-part-of-source'})

    def test_mcp_full_services_auth_annotations_and_catalog(self):
        google = GoogleServices(Mail(), MemoryGoogleJournal(), request=FakeAPI())
        server = create_server(settings(), StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}), google=google)
        with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            response = rpc(client, 'tools/call', {'name': 'google_services_catalog', 'arguments': {}},
                           headers={'Accept': 'application/json, text/event-stream'})
            self.assertEqual(response.status_code, 401)
            tools = rpc(client, 'tools/list').json()['result']['tools']
            tools = {t['name']: t for t in tools}
            self.assertFalse(tools['google_services_execute']['annotations']['readOnlyHint'])
            self.assertTrue(tools['google_services_execute']['annotations']['destructiveHint'])
            self.assertTrue(tools['google_services_read']['annotations']['readOnlyHint'])
            result = rpc(client, 'tools/call', {'name': 'google_services_catalog', 'arguments': {'service': 'tasks'}}).json()['result']
            self.assertEqual(len(result['structuredContent']['operations']), 14)
            read = rpc(client, 'tools/call', {'name': 'google_services_read', 'arguments': {
                'operation': 'tasks.tasklists.delete', 'params': {'tasklist': 'x'}}}).json()['result']
            self.assertTrue(read['isError'])


if __name__ == '__main__':
    unittest.main()
