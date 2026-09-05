import asyncio
import copy
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import ValidationError
from starlette.testclient import TestClient

from bridge.control import (ACCEPT, DISPATCH, EVIDENCE, PR, REQUEST, AcceptInput,
                            ControlPlane, ControlPolicy, DispatchInput, fenced, payload)
from bridge.core import BridgeError, Settings
from bridge.mcp_server import create_server
from test_mcp import AUTH, rpc

REPO = 'owner/private'
SHA = 'a' * 40
USER = {'login': 'owner', 'id': 123}


class Journal:
    def __init__(self):
        self.values = {}

    async def claim(self, key, fingerprint):
        if key in self.values:
            if self.values[key] != fingerprint:
                raise BridgeError('idempotency_conflict', 409)
            return False
        self.values[key] = fingerprint
        return True


class GitHub:
    def __init__(self):
        self.issues, self.prs, self.comments, self.calls = {}, {}, {}, []
        self.fail_post = False
        self.ambiguous = False
        self.repo_response = {'full_name': REPO, 'private': True}
        self.user = USER

    async def fetch(self, url, headers):
        self.calls.append(('GET', url, None))
        assert headers['Authorization'] == 'Bearer server-secret'
        path = urlsplit(url).path
        if path == '/user':
            return self.user
        root = '/repos/' + REPO
        if path == root:
            return self.repo_response
        rest = path.removeprefix(root)
        if rest == '/issues':
            return copy.deepcopy(list(self.issues.values()))
        if rest == '/pulls':
            return copy.deepcopy(list(self.prs.values()))
        parts = rest.split('/')
        if parts[1] == 'issues':
            n = int(parts[2])
            if len(parts) == 4:
                return copy.deepcopy(self.comments.get(n, []))
            return copy.deepcopy(self.issues[n])
        if parts[1] == 'pulls':
            if len(parts) == 4:
                return [{'filename': 'assets/test.txt', 'status': 'added', 'patch': '@@ -0,0 +1 @@\n+test'}]
            return copy.deepcopy(self.prs[int(parts[2])])
        if rest.endswith('check-runs'):
            return {'check_runs': []}
        if rest == '/actions/runs':
            return {'workflow_runs': []}
        if rest.startswith('/branches/'):
            return {'protected': False}
        if rest.startswith('/rules/branches/'):
            return []
        if rest.endswith('statuses'):
            return []
        raise AssertionError(url)

    async def send(self, method, url, headers, body):
        assert method == 'POST'
        assert headers['Authorization'] == 'Bearer server-secret'
        self.calls.append((method, url, copy.deepcopy(body)))
        if self.fail_post:
            raise BridgeError('github_request_failed', 502)
        rest = urlsplit(url).path.removeprefix('/repos/' + REPO)
        if rest == '/issues':
            n = len(self.issues) + 1
            result = {'number': n, 'html_url': f'https://github.com/{REPO}/issues/{n}',
                      'created_at': '2026-09-06T00:00:00Z', 'state': 'open', 'user': USER,
                      'title': body['title'], 'body': body['body'], 'labels': []}
            self.issues[n] = result
            if self.ambiguous:
                self.ambiguous = False
                raise BridgeError('github_request_failed', 502)
            return copy.deepcopy(result)
        n = int(rest.split('/')[2])
        if rest.endswith('/labels'):
            self.issues[n]['labels'] = [{'name': s} for s in body['labels']]
            return self.issues[n]['labels']
        if rest.endswith('/comments'):
            self.comments.setdefault(n, []).append({'body': body['body'], 'user': USER})
            return {'body': body['body']}
        raise AssertionError(url)


def fixture():
    cfg = Settings('k' * 40, {REPO: {'ref': 'main', 'program_prefixes': ['skills'], 'data_prefixes': ['docs']}}, 'server-secret')
    policy = ControlPolicy({'trusted_user_id': '123', 'trusted_login': 'owner', 'central_repository': REPO,
        'repositories': {REPO: {'target_branch': 'main', 'labels': ['documentation'],
                              'modes': ['controlled', 'maintainer'], 'required_sources': ['AGENTS.md']}}}, cfg)
    fake = GitHub()
    control = ControlPlane(cfg, policy, Journal(), fetch=fake.fetch, send=fake.send)
    return cfg, fake, control


def request(**overrides):
    return DispatchInput(**{**dict(task_repository=REPO, target_repository=REPO, title='測試派工',
        task='Create only assets/test.txt containing test.', allowed_paths=['assets/test.txt'],
        sources=[], validation_profile='documentation', commit_message='Test dispatch',
        idempotency_key='test-request-0001'), **overrides})


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg, self.fake, self.control = fixture()

    async def ready(self):
        result = await self.control.dispatch(request())
        n = result['issue_number']
        data = {'task_repository': REPO, 'task_issue': n, 'target_repository': REPO,
                'run_id': 'run-1', 'commit_sha': SHA}
        self.fake.prs[3] = {'number': 3, 'html_url': f'https://github.com/{REPO}/pull/3',
            'body': fenced(PR, data), 'head': {'sha': SHA}, 'base': {'ref': 'main', 'sha': 'b'*40, 'repo': {'full_name': REPO}},
            'draft': False, 'state': 'open', 'merged': False, 'changed_files': 1}
        self.fake.comments[n] = [{'user': USER, 'body': fenced(EVIDENCE, {
            'repository': REPO, 'issue_number': n, 'target_repository': REPO,
            'run_id': 'run-1', 'outcome': 'PASS', 'stage': 'completed',
            'provider_started': True, 'commit_sha': SHA})}]
        return AcceptInput(task_repository=REPO, task_issue=n, target_repository=REPO,
                           expected_commit_sha=SHA, expected_outcome='PASS')

    async def test_dispatch_contract_labels_and_readback(self):
        result = await self.control.dispatch(request())
        self.assertEqual(result['marker'], REQUEST)
        task = self.fake.issues[1]
        contract = payload(task['body'], REQUEST)
        self.assertEqual(contract['target_repository'], REPO)
        self.assertEqual(contract['paths'], ['assets/test.txt'])
        self.assertEqual(contract['sources'], ['AGENTS.md'])
        self.assertEqual(contract['profile'], 'documentation')
        self.assertEqual(contract['mode'], 'controlled')
        self.assertEqual(task['labels'], [{'name': 'local-coding-request'}])
        self.assertEqual(payload(self.fake.issues[2]['body'], DISPATCH),
                         {'task_repository': REPO, 'task_issue': 1, 'target_repository': REPO})
        self.assertEqual(self.fake.issues[2]['labels'], [{'name': 'local-coding-dispatch'}])

    async def test_dispatch_replay_and_concurrent_claim(self):
        results = await asyncio.gather(self.control.dispatch(request()), self.control.dispatch(request()))
        self.assertEqual({r['issue_number'] for r in results}, {1})
        self.assertEqual(len(self.fake.issues), 2)

    async def test_idempotency_conflicting_request(self):
        await self.control.dispatch(request())
        with self.assertRaisesRegex(BridgeError, 'idempotency_conflict'):
            await self.control.dispatch(request(task='Different intent'))
        self.assertEqual(len(self.fake.issues), 2)

    async def test_ambiguous_post_reconciles_without_duplicate(self):
        self.fake.ambiguous = True
        with self.assertRaises(BridgeError):
            await self.control.dispatch(request())
        result = await self.control.dispatch(request())
        self.assertEqual(result['status'], 'existing')
        self.assertEqual(len(self.fake.issues), 2)

    async def test_failed_post_never_blindly_replayed(self):
        self.fake.fail_post = True
        with self.assertRaises(BridgeError):
            await self.control.dispatch(request())
        self.fake.fail_post = False
        with self.assertRaisesRegex(BridgeError, 'creation_pending_or_indeterminate'):
            await self.control.dispatch(request())
        self.assertEqual(self.fake.issues, {})

    async def test_repo_rejected_before_network(self):
        with self.assertRaisesRegex(BridgeError, 'repository_not_allowed'):
            await self.control.dispatch(request(task_repository='evil/repo'))
        self.assertEqual(self.fake.calls, [])

    async def test_server_credential_identity_checked(self):
        self.fake.user = {'id': 456, 'login': 'owner'}
        with self.assertRaisesRegex(BridgeError, 'control_identity_not_trusted'):
            await self.control.dispatch(request())
        self.assertEqual(self.fake.issues, {})

    async def test_public_repo_rejected(self):
        self.fake.repo_response['private'] = False
        with self.assertRaisesRegex(BridgeError, 'private_repository_required'):
            await self.control.dispatch(request())

    async def test_accept_receipt_and_no_merge(self):
        r = await self.ready()
        first = await self.control.accept(r)
        second = await self.control.accept(r)
        self.assertEqual(first['issue_number'], second['issue_number'])
        self.assertEqual(second['status'], 'existing')
        issue = self.fake.issues[first['issue_number']]
        self.assertEqual(payload(issue['body'], ACCEPT), {'target_repository': REPO, 'target_issue': 1,
            'expected_outcome': 'PASS', 'expected_commit_sha': SHA})
        self.assertEqual(issue['labels'], [{'name': 'local-coding-dispatch'}])
        self.assertFalse(any('/merge' in url or '/git/' in url for _, url, _ in self.fake.calls))
        self.assertEqual(self.fake.issues[1]['state'], 'open')

    async def test_accept_retry_after_merge_and_close(self):
        r = await self.ready()
        first = await self.control.accept(r)
        self.fake.prs[3].update(merged=True, state='closed')
        self.fake.issues[1]['state'] = 'closed'
        self.fake.issues[first['issue_number']]['state'] = 'closed'
        again = await self.control.accept(r)
        self.assertEqual(again['issue_number'], first['issue_number'])
        self.assertEqual(len(self.fake.issues), 3)

    async def test_evidence_mismatch_blocks_ticket(self):
        for field, value in [('outcome', 'FAIL'), ('commit_sha', 'c'*40), ('target_repository', 'evil/repo'),
                             ('repository', 'evil/repo'), ('issue_number', 999), ('provider_started', False), ('stage', 'coding')]:
            with self.subTest(field=field):
                self.cfg, self.fake, self.control = fixture()
                r = await self.ready()
                evidence = payload(self.fake.comments[1][0]['body'], EVIDENCE)
                evidence[field] = value
                self.fake.comments[1][0]['body'] = fenced(EVIDENCE, evidence)
                with self.assertRaises(BridgeError):
                    await self.control.accept(r)
                self.assertEqual(len(self.fake.issues), 2)

    async def test_untrusted_evidence_blocks_ticket(self):
        r = await self.ready()
        self.fake.comments[1][0]['user'] = {'login': 'attacker', 'id': 999}
        with self.assertRaisesRegex(BridgeError, 'evidence_author_not_trusted'):
            await self.control.accept(r)

    async def test_pr_mismatch_blocks_ticket(self):
        for modification in ('draft', 'sha', 'base', 'task', 'target', 'run', 'missing', 'duplicate'):
            with self.subTest(modification=modification):
                self.cfg, self.fake, self.control = fixture()
                r = await self.ready()
                pr = self.fake.prs[3]
                if modification == 'draft': pr['draft'] = True
                elif modification == 'sha': pr['head']['sha'] = 'd'*40
                elif modification == 'base': pr['base']['ref'] = 'other'
                elif modification == 'missing': self.fake.prs.clear()
                elif modification == 'duplicate': self.fake.prs[4] = copy.deepcopy(pr)
                else:
                    p = payload(pr['body'], PR)
                    p[{'task':'task_issue','target':'target_repository','run':'run_id'}[modification]] = 'wrong'
                    pr['body'] = fenced(PR, p)
                with self.assertRaises(BridgeError): await self.control.accept(r)
                self.assertEqual(len(self.fake.issues), 2)

    async def test_pr_review_diff_checks_and_sha(self):
        await self.ready()
        review = await self.control.pr_review(REPO, 3)
        self.assertEqual(review['reviewed_commit_sha'], SHA)
        self.assertTrue(review['patches_complete'])
        self.assertEqual(review['files'][0]['filename'], 'assets/test.txt')
        self.assertEqual(review['check_runs'], [])
        self.assertEqual(review['statuses'], [])

    async def test_incomplete_diff_and_changed_head_fail(self):
        await self.ready()
        self.fake.prs[3]['changed_files'] = 2
        with self.assertRaisesRegex(BridgeError, 'pr_files_incomplete'):
            await self.control.pr_review(REPO, 3)

    async def test_ordinary_primitives_and_reserved_control(self):
        receipt = await self.control.create_issue(REPO, 'ordinary', 'body', 'ordinary-1')
        await self.control.add_label(REPO, receipt['issue_number'], 'documentation')
        await self.control.add_comment(REPO, receipt['issue_number'], 'review note')
        self.assertEqual(len((await self.control.read_comments(REPO, 1))['comments']), 1)
        self.assertEqual((await self.control.read_issue(REPO, 1))['state'], 'open')
        for label in ('local-coding-dispatch', 'unknown'):
            with self.assertRaises(BridgeError): await self.control.add_label(REPO, 1, label)
        with self.assertRaises(BridgeError): await self.control.add_comment(REPO, 1, EVIDENCE)
        with self.assertRaises(BridgeError): await self.control.create_issue(REPO, 'title', ACCEPT, 'forged')

    def test_strict_input_and_marker_parser(self):
        for field in ('token', 'endpoint', 'method'):
            with self.assertRaises(ValidationError): request(**{field: 'forbidden'})
        for body in ('missing', PR+'\n```json\n{"a":1,"a":2}\n```', PR+PR):
            with self.assertRaises(BridgeError): payload(body, PR)


class ControlMCPTests(unittest.TestCase):
    def test_schema_auth_and_no_arbitrary_operations(self):
        cfg, fake, control = fixture()
        mcp = create_server(cfg, StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}), control=control)
        with TestClient(mcp.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            listing = rpc(client, 'tools/list').json()['result']['tools']
            names = {t['name'] for t in listing}
            self.assertEqual(len(names), 12)
            self.assertTrue({'list_runtime_targets','run_readonly_skill','run_write_skill',
                             'dispatch_local_agent','accept_local_agent_result'} <= names)
            self.assertFalse(any('merge' in n or 'endpoint' in n or 'ref' in n for n in names))
            bad = rpc(client, 'tools/call', {'name':'dispatch_local_agent', 'arguments':{'request':request().model_dump()}},
                      headers={'Accept':'application/json, text/event-stream'})
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(fake.calls, [])
            for extra in ('token','endpoint','method'):
                r = rpc(client, 'tools/call', {'name':'dispatch_local_agent',
                    'arguments':{'request':{**request().model_dump(),extra:'bad'}}}).json()['result']
                self.assertTrue(r['isError'])
                self.assertEqual(fake.calls, [])
            r = rpc(client, 'tools/call', {'name':'dispatch_local_agent',
                    'arguments':{'request':request().model_dump()}}).json()['result']
            self.assertFalse(r.get('isError'), r)
            self.assertEqual(r['structuredContent']['issue_number'], 1)

class AdditionalBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_missing_server_credential_and_policy_denied(self):
        cfg, _, control = fixture()
        cfg.github_token = ''
        with self.assertRaisesRegex(BridgeError, 'control_credential_missing'):
            ControlPlane(cfg, control.policy, Journal(), fetch=None, send=None)
        bad = {'trusted_user_id':'123','trusted_login':'owner','central_repository':'evil/repo',
               'repositories':control.policy.repositories}
        with self.assertRaisesRegex(BridgeError, 'invalid_control_policy'):
            ControlPolicy(bad, cfg)

    async def test_concurrent_inflight_does_not_post_twice(self):
        _, fake, control = fixture()
        started, finish = asyncio.Event(), asyncio.Event()
        send = fake.send
        async def delayed(method, url, headers, body):
            if url.endswith('/issues') and not fake.issues:
                started.set()
                await finish.wait()
            return await send(method, url, headers, body)
        control.send = delayed
        first = asyncio.create_task(control.dispatch(request()))
        await started.wait()
        with self.assertRaisesRegex(BridgeError, 'creation_pending_or_indeterminate'):
            await control.dispatch(request())
        finish.set()
        result = await first
        replay = await control.dispatch(request())
        self.assertEqual(result['issue_number'], replay['issue_number'])
        self.assertEqual(len(fake.issues), 2)

    async def test_redis_journal_atomic_and_persistent_instances(self):
        from bridge.control import RedisJournal
        values = {}
        class Redis:
            @classmethod
            def from_url(cls, *args, **kwargs): return cls()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def set(self, key, value, nx=False):
                self.assert_nx = nx
                if key in values: return False
                values[key] = value
                return True
            async def get(self, key): return values.get(key)
        with patch('redis.asyncio.Redis', Redis):
            self.assertTrue(await RedisJournal('rediss://test').claim('key', 'hash'))
            self.assertFalse(await RedisJournal('rediss://test').claim('key', 'hash'))
            with self.assertRaisesRegex(BridgeError, 'idempotency_conflict'):
                await RedisJournal('rediss://test').claim('key', 'other')

    async def test_pr_moves_while_reviewing(self):
        t = ControlTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def moving(url, headers):
            result = await fetch(url, headers)
            if '/statuses?' in url:
                t.fake.prs[3]['base']['sha'] = 'e'*40
            return result
        t.control.github.fetch_json = moving
        with self.assertRaisesRegex(BridgeError, 'pr_changed_during_read'):
            await t.control.pr_review(REPO, 3)

    async def test_api_read_failure_cannot_create_acceptance(self):
        t = ControlTests()
        await t.asyncSetUp()
        r = await t.ready()
        fetch = t.fake.fetch
        async def failing(url, headers):
            if '/comments?' in url: raise BridgeError('github_request_failed', 502)
            return await fetch(url, headers)
        t.control.github.fetch_json = failing
        with self.assertRaises(BridgeError): await t.control.accept(r)
        self.assertEqual(len(t.fake.issues), 2)

    async def test_control_credentials_still_absent_from_canonical_python(self):
        from bridge.execution import execute_subprocess
        import os
        # Exercise the actual child process with a parent-side control token.
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'BRIDGE_GITHUB_TOKEN':'server-secret','GH_TOKEN':'host-secret'}):
            root = Path(d)
            (root/'main.py').write_text('import os\ndef run(root, value):\n return {k:os.environ.get(k) for k in ("BRIDGE_GITHUB_TOKEN","GH_TOKEN")}\n')
            # Execution adapter's callable signature is shared with existing tests.
            from bridge.execution import execute_subprocess
            result = execute_subprocess({'main.py':(root/'main.py').read_bytes()}, 'main.py', {})
            self.assertEqual(result.result, {'BRIDGE_GITHUB_TOKEN':None,'GH_TOKEN':None})


class ChecksPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_forbidden_exposes_available_validation_and_rules(self):
        t = ControlTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def limited(url, headers):
            if 'check-runs' in url: raise BridgeError('github_forbidden', 502)
            return await fetch(url, headers)
        t.control.github.fetch_json = limited
        result = await t.control.pr_review(REPO, 3)
        self.assertEqual(result['check_runs_error'], 'github_forbidden')
        self.assertEqual(result['workflow_runs'], [])
        self.assertTrue(result['required_checks_satisfied'])
        self.assertEqual(result['required_checks'], [])
        async def required(url, headers):
            if '/rules/branches/' in url:
                return [{'type':'required_status_checks','parameters':{'required_status_checks':[{'context':'build','integration_id':123}]}}]
            return await limited(url, headers)
        t.control.github.fetch_json = required
        result = await t.control.pr_review(REPO, 3)
        self.assertFalse(result['required_checks_satisfied'])

    async def test_checks_network_failure_does_not_fall_back(self):
        t = ControlTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def broken(url, headers):
            if 'check-runs' in url: raise BridgeError('github_request_failed', 502)
            return await fetch(url, headers)
        t.control.github.fetch_json = broken
        with self.assertRaisesRegex(BridgeError, 'github_request_failed'):
            await t.control.pr_review(REPO, 3)
