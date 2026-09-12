import asyncio
import copy
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.github_service import GitHubService, GitHubPolicy, RECEIPT, fenced
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
    policy = GitHubPolicy({'credential_user_id': '123', 'repositories': {
        REPO: {'permissions': ['read', 'issues_write'], 'private_only': True}}}, cfg)
    fake = GitHub()
    service = GitHubService(cfg, policy, Journal(), fetch=fake.fetch, send=fake.send)
    return cfg, fake, service


class GitHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg, self.fake, self.service = fixture()

    async def ready(self):
        await self.service.create_issue(REPO, 'test', 'ordinary issue body', 'create-test-issue')
        self.fake.prs[3] = {'number': 3, 'html_url': f'https://github.com/{REPO}/pull/3',
            'body': 'Arbitrary application data', 'head': {'sha': SHA},
            'base': {'ref': 'main', 'sha': 'b'*40, 'repo': {'full_name': REPO}},
            'draft': False, 'state': 'open', 'merged': False, 'changed_files': 1}

    async def test_issue_replay_and_concurrent_claim(self):
            results = await asyncio.gather(self.service.create_issue(REPO, 'test', 'body', 'create-test-issue'), self.service.create_issue(REPO, 'test', 'body', 'create-test-issue'))
            self.assertEqual({r['issue_number'] for r in results}, {1})
            self.assertEqual(len(self.fake.issues), 1)

    async def test_idempotency_conflicting_request(self):
            await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            with self.assertRaisesRegex(BridgeError, 'idempotency_conflict'):
                await self.service.create_issue(REPO, 'test', 'different intent', 'create-test-issue')
            self.assertEqual(len(self.fake.issues), 1)

    async def test_ambiguous_post_reconciles_without_duplicate(self):
            self.fake.ambiguous = True
            with self.assertRaises(BridgeError):
                await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            result = await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            self.assertEqual(result['status'], 'existing')
            self.assertEqual(len(self.fake.issues), 1)

    async def test_failed_post_never_blindly_replayed(self):
            self.fake.fail_post = True
            with self.assertRaises(BridgeError):
                await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            self.fake.fail_post = False
            with self.assertRaisesRegex(BridgeError, 'creation_pending_or_indeterminate'):
                await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            self.assertEqual(self.fake.issues, {})

    async def test_repo_rejected_before_network(self):
            with self.assertRaisesRegex(BridgeError, 'repository_not_allowed'):
                await self.service.create_issue('evil/repo', 'test', 'body', 'create-test-issue')
            self.assertEqual(self.fake.calls, [])

    async def test_server_credential_identity_checked(self):
            self.fake.user = {'id': 456, 'login': 'owner'}
            with self.assertRaisesRegex(BridgeError, 'github_credential_identity_mismatch'):
                await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            self.assertEqual(self.fake.issues, {})

    async def test_public_repo_rejected(self):
            self.fake.repo_response['private'] = False
            with self.assertRaisesRegex(BridgeError, 'private_repository_required'):
                await self.service.create_issue(REPO, 'test', 'body', 'create-test-issue')

    async def test_pr_review_diff_checks_and_sha(self):
            await self.ready()
            review = await self.service.pr_review(REPO, 3)
            self.assertEqual(review['reviewed_commit_sha'], SHA)
            self.assertTrue(review['patches_complete'])
            self.assertEqual(review['files'][0]['filename'], 'assets/test.txt')
            self.assertEqual(review['check_runs'], [])
            self.assertEqual(review['statuses'], [])

    async def test_incomplete_diff_and_changed_head_fail(self):
            await self.ready()
            self.fake.prs[3]['changed_files'] = 2
            with self.assertRaisesRegex(BridgeError, 'pr_files_incomplete'):
                await self.service.pr_review(REPO, 3)

    async def test_generic_text_labels_and_pr_bodies_are_opaque(self):
        body = 'LOCAL_CODING_ACCEPTANCE_V1 invalid JSON; application-owned content'
        await self.service.create_issue(REPO, 'ordinary', body, 'opaque-content')
        await self.service.add_label(REPO, 1, 'local-coding-dispatch')
        await self.service.add_label(REPO, 1, 'new-workflow-label')
        await self.service.add_comment(REPO, 1, 'LOCAL_CODING_DISPATCH_EVIDENCE_V1 malformed')
        self.assertEqual(len((await self.service.read_comments(REPO, 1))['comments']), 1)
        self.assertEqual((await self.service.read_issue(REPO, 1))['state'], 'open')
        await self.ready()
        self.fake.prs[3]['body'] = 'LOCAL_AGENT_DISPATCH_PR_V1 malformed'
        result = await self.service.read_pr(REPO, 3)
        self.assertEqual(result['body'], self.fake.prs[3]['body'])
        self.assertNotIn('dispatch_payload', result)

    async def test_receipt_forgery_is_rejected_but_workflow_markers_are_not_reserved(self):
        with self.assertRaisesRegex(BridgeError, 'reserved_receipt_marker'):
            await self.service.create_issue(REPO, 'ordinary', fenced(RECEIPT, {'key': 'fake'}), 'opaque-content')
        self.assertEqual(self.fake.calls, [])
        await self.service.create_issue(REPO, 'ordinary', 'body', 'opaque-content')
        self.fake.issues[1]['user'] = {'id': 999, 'login': 'attacker'}
        with self.assertRaisesRegex(BridgeError, 'idempotency_receipt_mismatch'):
            await self.service.create_issue(REPO, 'ordinary', 'body', 'opaque-content')

    async def test_repository_permissions_are_checked_before_network(self):
        self.service.policy.repositories[REPO]['permissions'] = ['read']
        for call in [self.service.create_issue(REPO, 'ordinary', 'body', 'permission-case'),
                     self.service.add_label(REPO, 1, 'arbitrary'), self.service.add_comment(REPO, 1, 'body')]:
            with self.assertRaisesRegex(BridgeError, 'github_permission_denied'):
                await call
        self.assertEqual(self.fake.calls, [])

    async def test_generic_lists_and_bounds(self):
        await self.ready()
        self.fake.issues[9] = {'pull_request': {}, 'number': 9}
        self.assertEqual(len((await self.service.list_issues(REPO))['issues']), 1)
        self.assertEqual(len((await self.service.list_prs(REPO))['pull_requests']), 1)
        with self.assertRaisesRegex(BridgeError, 'invalid_pr_state'):
            await self.service.list_prs(REPO, 'all&state=open')
        async def full_page(url, headers):
            return [{}] * 100
        self.service.github.fetch_json = full_page
        with self.assertRaisesRegex(BridgeError, 'github_listing_truncated'):
            await self.service.pages('/repos/' + REPO + '/issues')


class GitHubMCPTests(unittest.TestCase):
    def test_auth_and_strict_schema_before_credential_use(self):
        cfg, fake, service = fixture()
        server = create_server(cfg, StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}), github=service)
        with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            arguments = {'repository': REPO, 'title': 'test', 'body': 'body', 'idempotency_key': 'test-ordinary'}
            bad = rpc(client, 'tools/call', {'name': 'create_github_issue', 'arguments': arguments},
                      headers={'Accept':'application/json, text/event-stream'})
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(fake.calls, [])
            for extra in ('token', 'endpoint', 'method'):
                result = rpc(client, 'tools/call', {'name': 'create_github_issue',
                    'arguments': {**arguments, extra: 'bad'}}).json()['result']
                self.assertTrue(result['isError'])
                self.assertEqual(fake.calls, [])
            result = rpc(client, 'tools/call', {'name': 'create_github_issue', 'arguments': arguments}).json()['result']
            self.assertFalse(result.get('isError'), result)
            self.assertEqual(result['structuredContent']['issue_number'], 1)


class AdditionalBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_missing_server_credential_and_unknown_policy_fail_closed(self):
        cfg, _, service = fixture()
        cfg.github_token = ''
        with self.assertRaisesRegex(BridgeError, 'github_credential_missing'):
            GitHubService(cfg, service.policy, Journal(), fetch=None, send=None)
        with self.assertRaisesRegex(BridgeError, 'invalid_github_policy'):
            GitHubPolicy({'credential_user_id': '123', 'repositories': service.policy.repositories,
                          'central_repository': REPO}, cfg)

    async def test_concurrent_inflight_does_not_post_twice(self):
            _, fake, service = fixture()
            started, finish = asyncio.Event(), asyncio.Event()
            send = fake.send
            async def delayed(method, url, headers, body):
                if url.endswith('/issues') and not fake.issues:
                    started.set()
                    await finish.wait()
                return await send(method, url, headers, body)
            service.send = delayed
            first = asyncio.create_task(service.create_issue(REPO, 'test', 'body', 'create-test-issue'))
            await started.wait()
            with self.assertRaisesRegex(BridgeError, 'creation_pending_or_indeterminate'):
                await service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            finish.set()
            result = await first
            replay = await service.create_issue(REPO, 'test', 'body', 'create-test-issue')
            self.assertEqual(result['issue_number'], replay['issue_number'])
            self.assertEqual(len(fake.issues), 1)

    async def test_redis_journal_atomic_and_persistent_instances(self):
            from bridge.github_service import RedisJournal
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
            t = GitHubTests()
            await t.asyncSetUp()
            await t.ready()
            fetch = t.fake.fetch
            async def moving(url, headers):
                result = await fetch(url, headers)
                if '/statuses?' in url:
                    t.fake.prs[3]['base']['sha'] = 'e'*40
                return result
            t.service.github.fetch_json = moving
            with self.assertRaisesRegex(BridgeError, 'pr_changed_during_read'):
                await t.service.pr_review(REPO, 3)

    async def test_credentials_still_absent_from_canonical_python(self):
            from bridge.execution import execute_subprocess
            import os
            # Exercise the actual child process with a parent-side service token.
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
        t = GitHubTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def limited(url, headers):
            if 'check-runs' in url: raise BridgeError('github_forbidden', 502)
            return await fetch(url, headers)
        t.service.github.fetch_json = limited
        result = await t.service.pr_review(REPO, 3)
        self.assertEqual(result['check_runs_error'], 'github_forbidden')
        self.assertEqual(result['workflow_runs'], [])
        self.assertTrue(result['required_checks_satisfied'])
        self.assertEqual(result['required_checks'], [])
        async def required(url, headers):
            if '/branches/' in url and '/rules/' not in url:
                return {'protected': True, 'protection': {'required_status_checks': {'contexts': [], 'checks': []}}}
            if '/rules/branches/' in url:
                return [{'type':'required_status_checks','parameters':{'required_status_checks':[{'context':'build','integration_id':123}]}}]
            return await limited(url, headers)
        t.service.github.fetch_json = required
        result = await t.service.pr_review(REPO, 3)
        self.assertFalse(result['required_checks_satisfied'])

    async def test_checks_network_failure_does_not_fall_back(self):
        t = GitHubTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def broken(url, headers):
            if 'check-runs' in url: raise BridgeError('github_request_failed', 502)
            return await fetch(url, headers)
        t.service.github.fetch_json = broken
        with self.assertRaisesRegex(BridgeError, 'github_request_failed'):
            await t.service.pr_review(REPO, 3)

    async def test_unprotected_branch_does_not_require_unavailable_rules_api(self):
        t = GitHubTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def unprotected(url, headers):
            if '/rules/branches/' in url:
                self.fail('Explicitly unprotected branch must not query unavailable rules API')
            return await fetch(url, headers)
        t.service.github.fetch_json = unprotected
        result = await t.service.pr_review(REPO, 3)
        self.assertTrue(result['required_checks_satisfied'])

    async def test_protected_branch_unavailable_rules_fails_closed(self):
        t = GitHubTests()
        await t.asyncSetUp()
        await t.ready()
        fetch = t.fake.fetch
        async def unavailable(url, headers):
            if '/rules/branches/' in url:
                raise BridgeError('github_forbidden', 502)
            if '/branches/' in url:
                return {'protected': True}
            return await fetch(url, headers)
        t.service.github.fetch_json = unavailable
        with self.assertRaisesRegex(BridgeError, 'github_forbidden'):
            await t.service.pr_review(REPO, 3)
