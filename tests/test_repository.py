import base64
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.core import BridgeError, Settings, safe_path
from bridge.mcp_server import create_server
from bridge.repository import Evidence, RepositoryService, digest, encoded
from bridge.validation import validation_profile
from test_mcp import AUTH, rpc

REPO = 'owner/repository'


class Journal:
    def __init__(self):
        self.values = {}

    async def claim(self, key, fingerprint):
        if key in self.values:
            if self.values[key] != fingerprint:
                raise BridgeError('idempotency_conflict')
            return False
        self.values[key] = fingerprint
        return True


class GitFixture:
    """Content-addressed Git objects and a moving branch, including lost responses."""
    def __init__(self, files):
        self.objects, self.trees, self.commits, self.snapshots = {}, {}, {}, {}
        self.calls, self.writes = [], []
        self.unknown = self.move_on_patch = self.before_post_error = False
        self.head = self.add_commit(files, [], 'initial')

    def tree(self, files):
        root = {}
        for path, content in files.items():
            parts = path.split('/')
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = content
        def walk(node):
            entries = []
            for name, value in sorted(node.items()):
                if isinstance(value, dict):
                    entry = {'path': name, 'type': 'tree', 'mode': '040000', 'sha': walk(value)}
                else:
                    content = value[1] if isinstance(value, tuple) else value
                    sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
                    self.objects[sha] = content
                    entry = {'path': name, 'type': 'blob', 'mode': value[0] if isinstance(value, tuple) else '100644',
                             'sha': sha, 'size': len(content)}
                entries.append(entry)
            sha = hashlib.sha1(encoded(entries)).hexdigest()
            self.trees[sha] = {'tree': entries}
            return sha
        sha = walk(root)
        self.snapshots[sha] = deepcopy(files)
        return sha

    def add_commit(self, files, parents, message):
        tree = self.tree(files)
        commit = {'tree': {'sha': tree}, 'parents': [{'sha': p} for p in parents], 'message': message}
        sha = hashlib.sha1(encoded(commit)).hexdigest()
        self.commits[sha] = {**commit, 'sha': sha}
        return sha

    @property
    def files(self):
        return self.snapshots[self.commits[self.head]['tree']['sha']]

    async def fetch(self, url, headers):
        assert headers['Authorization'] == 'Bearer host-secret'
        path = url.split('/repos/' + REPO)[1]
        self.calls.append(path)
        if path.startswith('/git/ref/heads/'):
            return {'object': {'sha': self.head}}
        if path.startswith('/git/commits/'):
            return deepcopy(self.commits[path.split('/')[-1]])
        if path.startswith('/git/trees/'):
            sha = path.split('/')[-1].split('?')[0]
            if '?recursive=1' not in path:
                return deepcopy(self.trees[sha])
            entries = []
            def walk(tree, prefix=''):
                for entry in self.trees[tree]['tree']:
                    item = {**entry, 'path': prefix + entry['path']}
                    entries.append(item)
                    if entry['type'] == 'tree':
                        walk(entry['sha'], item['path'] + '/')
            walk(sha)
            return {'sha': sha, 'tree': entries, 'truncated': False}
        if path.startswith('/git/blobs/'):
            return {'encoding': 'base64', 'content': base64.b64encode(self.objects[path.split('/')[-1]]).decode()}
        if path.startswith('/compare/'):
            base, head = path.split('/')[-1].split('...')
            before = self.snapshots[self.commits[base]['tree']['sha']]
            after = self.snapshots[self.commits[head]['tree']['sha']]
            return {'files': [{'filename': p} for p in before.keys() | after.keys() if before.get(p) != after.get(p)]}
        raise AssertionError(path)

    async def send(self, method, url, headers, body):
        self.writes.append((method, url, body))
        if self.before_post_error:
            raise BridgeError('transport_unavailable')
        if url.endswith('/trees'):
            files = deepcopy(self.snapshots[body['base_tree']])
            for entry in body['tree']:
                if entry.get('sha', 'content') is None:
                    del files[entry['path']]
                else:
                    files[entry['path']] = entry['content'].encode()
            return {'sha': self.tree(files)}
        if url.endswith('/commits'):
            return {'sha': self.add_commit(self.snapshots[body['tree']], body['parents'], body['message'])}
        if '/refs/heads/' in url:
            if self.move_on_patch:
                self.head = self.add_commit({**self.files, 'other.txt': b'concurrent'}, [self.head], 'concurrent')
            if self.commits[body['sha']]['parents'] != [{'sha': self.head}]:
                raise BridgeError('branch_conflict', 409)
            self.head = body['sha']
            if self.unknown:
                self.unknown = False
                raise BridgeError('transport_unavailable')
            return {'object': {'sha': self.head}}
        raise AssertionError(url)


def fixture():
    profile = {'version': 1, 'profiles': {'tests': {'files': ['src/', 'tests/'],
               'commands': [{'executable': 'python', 'argv': ['-m', 'unittest', 'discover', '-s', 'tests']}]}}}
    git = GitFixture({'src/calc.py': b'def add(a, b):\n    return a - b\n',
        'tests/test_calc.py': b'import unittest\nfrom src.calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2, 3), 5)\n',
        'validation.json': encoded(profile), 'notes.txt': b'unchanged\n'})
    settings = Settings('k' * 40, {REPO: {'ref': 'main', 'program_prefixes': ['src', 'tests'],
                        'read_all': True, 'write_refs': ['main'], 'write_all_refs': ['main']}}, 'host-secret')
    observations = []
    async def validate(files, selected):
        # Run this test's trusted fixture through the existing real child runtime.
        # Production validation always uses the separate microVM adapter.
        from bridge.execution import execute_subprocess
        driver = b'import sys, unittest\ndef run(root, request):\n sys.path.insert(0,root)\n suite=unittest.defaultTestLoader.discover(root+"/tests")\n r=unittest.TestResult()\n suite.run(r)\n return {"passed":r.wasSuccessful(),"tests":r.testsRun,"complete":True,"truncated":False}\n'
        observations.append(deepcopy(files))
        return execute_subprocess({**files, '_fixture.py': driver}, '_fixture.py', {}).result
    service = RepositoryService(settings, fetch=git.fetch, send=git.send, journal=Journal(),
                                evidence=Evidence('e' * 40), validator=validate)
    return git, settings, service, observations


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.git, self.settings, self.service, self.observations = fixture()
        self.base = self.git.head
        source = self.git.files['src/calc.py']
        offset = source.decode().index('-')
        self.candidate = {'base_commit': self.base, 'changes': [{'path': 'src/calc.py', 'operation': 'update',
            'expected_sha256': digest(source), 'edits': [{'start': offset, 'end': offset + 1, 'expected': '-', 'replacement': '+'}]}]}

    async def evaluate(self, operation='inspect', candidate=None, **kwargs):
        return await self.service.evaluate(REPO, 'main', candidate or self.candidate, operation, **kwargs)

    async def evidence(self):
        inspection = await self.evaluate()
        self.candidate['fingerprint'] = inspection['candidate_fingerprint']
        validation = await self.evaluate('validate', manifest='validation.json', profile='tests')
        self.assertTrue(validation['passed'])
        return inspection['inspection_receipt'], validation['validation_receipt']

    async def commit(self, receipts):
        return await self.service.persist(REPO, 'main', self.candidate, 'Fix addition', *receipts)

    async def test_full_candidate_loop_real_overlay_validation_diff_commit_readback(self):
        search = await self.service.query(REPO, 'main', {'operation': 'search', 'pattern': 'return', 'suffix': '.py'})
        self.assertEqual(search['resolved_commit'], self.base)
        self.assertEqual(search['items'][0]['line'], 2)
        read = await self.service.query(REPO, self.base, {'operation': 'read', 'path': 'src/calc.py'})
        self.assertEqual(read['sha256'], self.candidate['changes'][0]['expected_sha256'])
        receipts = await self.evidence()
        self.assertEqual(self.git.head, self.base)
        self.assertIn(b'a + b', self.observations[0]['src/calc.py'])
        inspect = await self.evaluate()
        self.assertIn('-    return a - b', inspect['diff'])
        self.assertIn('+    return a + b', inspect['diff'])
        result = await self.commit(receipts)
        self.assertEqual(result['candidate_fingerprint'], inspect['candidate_fingerprint'])
        self.assertTrue(result['readback_verified'])
        readback = await self.service.query(REPO, result['commit'], {'operation': 'read', 'path': 'src/calc.py'})
        self.assertEqual(readback['text'].encode(), self.observations[0]['src/calc.py'])
        self.assertEqual(self.git.files['notes.txt'], b'unchanged\n')
        again = await self.commit(receipts)
        self.assertEqual(again['commit'], result['commit'])
        self.assertTrue(again['replayed'])
        self.assertEqual(len(self.git.writes), 3)

    async def test_stale_base_fails_before_new_claim_or_write(self):
        receipts = await self.evidence()
        self.git.head = self.git.add_commit({**self.git.files, 'new.txt': b'other'}, [self.base], 'Other change')
        with self.assertRaisesRegex(BridgeError, 'branch_conflict'):
            await self.commit(receipts)
        self.assertEqual(self.git.writes, [])
        self.assertEqual(self.service.journal.values, {})

    async def test_concurrent_ref_update_fails_closed(self):
        receipts = await self.evidence()
        self.git.move_on_patch = True
        with self.assertRaisesRegex(BridgeError, 'branch_conflict'):
            await self.commit(receipts)
        self.assertIn(b'a - b', self.git.files['src/calc.py'])

    async def test_unknown_write_retry_recovers_actual_remote_commit(self):
        receipts = await self.evidence()
        self.git.unknown = True
        with self.assertRaisesRegex(BridgeError, 'transport_unavailable'):
            await self.commit(receipts)
        result = await self.commit(receipts)
        self.assertTrue(result['replayed'])
        self.assertEqual(len(self.git.writes), 3)

    async def test_unknown_before_effect_never_blindly_retries(self):
        receipts = await self.evidence()
        self.git.before_post_error = True
        with self.assertRaises(BridgeError):
            await self.commit(receipts)
        count = len(self.git.writes)
        self.git.before_post_error = False
        with self.assertRaisesRegex(BridgeError, 'commit_pending_or_indeterminate'):
            await self.commit(receipts)
        self.assertEqual(len(self.git.writes), count)

    async def test_retry_after_later_commit_finds_exact_original_receipt(self):
        receipts = await self.evidence()
        original = await self.commit(receipts)
        self.git.head = self.git.add_commit({**self.git.files, 'new.txt': b'other'}, [self.git.head], 'Later')
        self.assertEqual((await self.commit(receipts))['commit'], original['commit'])

    async def test_candidate_hash_and_evidence_mismatches(self):
        receipts = await self.evidence()
        self.candidate['changes'][0]['edits'][0]['replacement'] = '*'
        with self.assertRaisesRegex(BridgeError, 'candidate_fingerprint_mismatch'):
            await self.evaluate()
        del self.candidate['fingerprint']
        inspection = await self.evaluate()
        self.candidate['fingerprint'] = inspection['candidate_fingerprint']
        with self.assertRaisesRegex(BridgeError, 'candidate_evidence_mismatch'):
            await self.commit(receipts)
        self.assertEqual(self.git.writes, [])

    async def test_expired_and_forged_evidence(self):
        receipts = await self.evidence()
        with self.assertRaisesRegex(BridgeError, 'candidate_evidence_mismatch'):
            await self.commit((receipts[0] + 'x', receipts[1]))
        with patch('bridge.repository.time.time', return_value=10**12):
            with self.assertRaisesRegex(BridgeError, 'candidate_evidence_mismatch'):
                await self.commit(receipts)

    async def test_stale_sha_patch_mismatch_create_existing_delete_missing(self):
        cases = []
        bad = deepcopy(self.candidate); bad['changes'][0]['expected_sha256'] = '0' * 64; cases.append(bad)
        bad = deepcopy(self.candidate); bad['changes'][0]['edits'][0]['expected'] = 'wrong'; cases.append(bad)
        bad = deepcopy(self.candidate); bad['changes'][0]['operation'] = 'create'; cases.append(bad)
        cases.append({'base_commit': self.base, 'changes': [{'path': 'missing.txt', 'operation': 'delete', 'expected_sha256': None}]})
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(BridgeError):
                    await self.evaluate(candidate=candidate)
        self.assertEqual(self.git.writes, [])

    async def test_create_delete_and_no_newline_diff(self):
        self.candidate['changes'] = [{'path': 'created.txt', 'operation': 'create', 'expected_sha256': None, 'content': 'new'},
            {'path': 'notes.txt', 'operation': 'delete', 'expected_sha256': digest(b'unchanged\n')}]
        result = await self.evaluate()
        self.assertIn('+++ /dev/null', result['diff'])
        self.assertIn('\\ No newline at end of file', result['diff'])
        self.assertEqual({c['operation'] for c in result['changed_files']}, {'create', 'delete'})

    async def test_fingerprint_stable_for_equivalent_exact_representations(self):
        first = await self.evaluate()
        del self.candidate['changes'][0]['edits']
        self.candidate['changes'][0]['content'] = 'def add(a, b):\n    return a + b\n'
        self.assertEqual((await self.evaluate())['candidate_fingerprint'], first['candidate_fingerprint'])

    async def test_bounded_tree_search_and_reads(self):
        tree = await self.service.query(REPO, 'main', {'operation': 'tree', 'max_entries': 1})
        self.assertFalse(tree['complete'])
        search = await self.service.query(REPO, 'main', {'operation': 'search', 'pattern': ' ', 'max_results': 1})
        self.assertLessEqual(len(search['items']), 1)
        self.assertTrue(search['truncated'])
        self.git.head = self.git.add_commit({**self.git.files, 'large.txt': ('字' * 10000).encode()}, [self.base], 'large')
        read = await self.service.query(REPO, 'main', {'operation': 'read', 'path': 'large.txt', 'max_bytes': 1024})
        self.assertTrue(read['truncated'])
        self.assertLessEqual(len(encoded(read)), 1024)
        ranged = await self.service.query(REPO, 'main', {'operation': 'read', 'path': 'src/calc.py', 'start_line': 2, 'end_line': 2})
        self.assertEqual(ranged['text'], '    return a - b\n')
        self.assertTrue(ranged['complete']); self.assertFalse(ranged['whole_file'])

    async def test_paths_symlinks_submodules_bad_ref_and_permission(self):
        for path in ('../../etc/passwd', '/etc/passwd', '.git/config', 'src/../a', 'src\\a'):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                await self.service.query(REPO, 'main', {'operation': 'read', 'path': path})
        for ref in ('unknown', '../../main'):
            with self.assertRaises(BridgeError):
                await self.service.query(REPO, ref, {'operation': 'tree'})
        for mode in ('120000', '160000'):
            self.git.head = self.git.add_commit({**self.git.files, 'unsafe': (mode, b'/etc/passwd')}, [self.git.head], 'unsafe')
            with self.assertRaises(BridgeError):
                await self.service.query(REPO, 'main', {'operation': 'read', 'path': 'unsafe'})
        self.settings.repositories[REPO]['read_all'] = False
        with self.assertRaises(BridgeError):
            await self.service.query(REPO, 'main', {'operation': 'read', 'path': 'notes.txt'})

    async def test_failed_validation_and_truncated_inspection_have_no_evidence(self):
        self.candidate['changes'][0]['edits'][0]['replacement'] = '*'
        result = await self.evaluate('validate', manifest='validation.json', profile='tests')
        self.assertFalse(result['passed']); self.assertNotIn('validation_receipt', result)
        self.candidate['changes'] = [{'path': 'big.txt', 'operation': 'create', 'expected_sha256': None, 'content': 'x\n' * 5000}]
        result = await self.evaluate(max_bytes=1024)
        self.assertTrue(result['truncated']); self.assertNotIn('inspection_receipt', result)

    async def test_manifest_change_only_changes_repository_owned_test_selection(self):
        receipts = await self.evidence()
        manifest = json.loads(self.git.files['validation.json'])
        manifest['profiles']['tests']['commands'][0]['argv'] += ['-v']
        self.candidate['changes'].append({'path': 'validation.json', 'operation': 'update',
            'expected_sha256': digest(self.git.files['validation.json']), 'content': json.dumps(manifest)})
        del self.candidate['fingerprint']
        updated = await self.evaluate('validate', manifest='validation.json', profile='tests')
        self.assertTrue(updated['passed'])
        self.assertNotEqual(updated['validation_receipt'], receipts[1])
        self.assertEqual(json.loads(self.observations[-1]['validation.json']), manifest)

    async def test_validation_selects_new_candidate_file_and_directory(self):
        self.candidate['changes'].append({'path': 'src/new/module.py', 'operation': 'create',
                                         'expected_sha256': None, 'content': 'value = 1\n'})
        manifest = json.loads(self.git.files['validation.json'])
        manifest['profiles']['tests']['files'] += ['src/new/', 'src/new/module.py']
        self.candidate['changes'].append({'path': 'validation.json', 'operation': 'update',
            'expected_sha256': digest(self.git.files['validation.json']), 'content': json.dumps(manifest)})
        result = await self.evaluate('validate', manifest='validation.json', profile='tests')
        self.assertTrue(result['passed'])
        self.assertEqual(self.observations[-1]['src/new/module.py'], b'value = 1\n')


class ManifestTests(unittest.TestCase):
    def test_forbidden_execution_and_path_contracts(self):
        cases = [ {'executable': 'bash', 'argv': ['-c', 'anything']},
            {'executable': 'python', 'argv': 'python test.py'},
            {'executable': 'python', 'argv': ['-c', 'print(1)']},
            {'executable': 'python', 'argv': ['/tmp/test.py']},
            {'executable': 'python', 'argv': ['test.py'], 'cwd': '../outside'},
            {'executable': 'python', 'argv': ['test.py', '--file=../../etc/passwd']},
            {'executable': 'python', 'argv': ['test.py'], 'env': {'SECRET': 'no'}},
            {'executable': 'python', 'argv': ['test.py'], 'shell': True}]
        for command in cases:
            with self.subTest(command=command), self.assertRaises(BridgeError):
                validation_profile({'version': 1, 'profiles': {'test': {'files': ['src/'], 'commands': [command]}}}, 'test')

    def test_profile_limits(self):
        good = {'files': ['src/'], 'commands': [{'executable': 'python', 'argv': ['-m', 'unittest']}]}
        for field, value in [('timeout_seconds', 31), ('output_bytes', 1000000), ('files', ['../']), ('commands', good['commands'] * 5)]:
            with self.subTest(field=field), self.assertRaises(BridgeError):
                validation_profile({'version': 1, 'profiles': {'x': {**good, field: value}}}, 'x')

    def test_safe_path(self):
        self.assertEqual(safe_path('src/code.py'), 'src/code.py')


class RepositoryMCPTests(unittest.TestCase):
    def test_real_mcp_schema_query_inspect_validate_commit_and_unknown_fields(self):
        git, settings, service, _ = fixture()
        server = create_server(settings, StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}), repository_service=service)
        with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            def call(name, **arguments):
                result = rpc(client, 'tools/call', {'name': name, 'arguments': arguments}).json()['result']
                self.assertFalse(result.get('isError'), result)
                return result['structuredContent']
            read = call('query_repository', repository=REPO, ref='main', query={'operation': 'read', 'path': 'src/calc.py'})
            candidate = {'base_commit': read['resolved_commit'], 'changes': [{'path': 'src/calc.py', 'operation': 'update',
                'expected_sha256': read['sha256'], 'content': read['text'].replace('a - b', 'a + b')}]}
            inspection = call('evaluate_repository_candidate', repository=REPO, ref='main', candidate=candidate, operation='inspect')
            candidate['fingerprint'] = inspection['candidate_fingerprint']
            validation = call('evaluate_repository_candidate', repository=REPO, ref='main', candidate=candidate,
                              operation='validate', manifest='validation.json', profile='tests')
            result = call('commit_repository_candidate', repository=REPO, ref='main', candidate=candidate,
                message='Fix addition', inspection_receipt=inspection['inspection_receipt'], validation_receipt=validation['validation_receipt'])
            self.assertEqual(result['commit'], git.head)
            invalid = rpc(client, 'tools/call', {'name': 'query_repository', 'arguments': {'repository': REPO,
                'ref': 'main', 'query': {'operation': 'search', 'pattern': 'x', 'shell': 'rg x'}}}).json()['result']
            self.assertTrue(invalid['isError'])


class RepositoryHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_transport_shares_exact_candidate_service(self):
        from bridge.repository_http import handle_repository
        git, settings, service, _ = fixture()
        async def call(tool_name, **arguments):
            status, value = await handle_repository(encoded({'operation': tool_name, 'arguments': arguments}),
                'Bearer ' + settings.key, service)
            self.assertEqual(status, 200, value)
            return value['result']
        read = await call('query_repository', repository=REPO, ref='main', query={'operation': 'read', 'path': 'src/calc.py'})
        candidate = {'base_commit': read['resolved_commit'], 'changes': [{'operation': 'update', 'path': 'src/calc.py',
            'expected_sha256': read['sha256'], 'content': read['text'].replace('a - b', 'a + b')}]}
        inspection = await call('evaluate_repository_candidate', repository=REPO, ref='main', candidate=candidate, operation='inspect')
        candidate['fingerprint'] = inspection['candidate_fingerprint']
        validation = await call('evaluate_repository_candidate', repository=REPO, ref='main', candidate=candidate,
            operation='validate', manifest='validation.json', profile='tests')
        result = await call('commit_repository_candidate', repository=REPO, ref='main', candidate=candidate,
            message='Fix addition', inspection_receipt=inspection['inspection_receipt'], validation_receipt=validation['validation_receipt'])
        self.assertEqual(result['commit'], git.head)

    async def test_http_auth_before_data_and_network_and_no_service_escalation(self):
        from bridge.repository_http import handle_repository
        git, settings, service, _ = fixture()
        status, _ = await handle_repository(b'not-json', '', service)
        self.assertEqual(status, 401); self.assertEqual(git.calls, [])
        status, result = await handle_repository(encoded({'operation': 'google_services_execute', 'arguments': {}}),
            'Bearer ' + settings.key, service)
        self.assertEqual(status, 400)
        self.assertEqual(result['error']['code'], 'unsupported_repository_operation')
        self.assertEqual(git.calls, [])

    async def test_http_size_and_schema_limits(self):
        from bridge.repository import MAX_INPUT
        from bridge.repository_http import handle_repository
        git, settings, service, _ = fixture()
        status, _ = await handle_repository(b'x' * (MAX_INPUT + 1), 'Bearer ' + settings.key, service)
        self.assertEqual(status, 413)
        status, _ = await handle_repository(encoded({'operation': 'query_repository', 'arguments': {
            'repository': REPO, 'ref': 'main', 'query': {'operation': 'tree'}, 'shell': True}}),
            'Bearer ' + settings.key, service)
        self.assertEqual(status, 400); self.assertEqual(git.calls, [])
