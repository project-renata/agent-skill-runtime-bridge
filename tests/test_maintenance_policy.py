"""Generic host-enforced required validation and repository-owned drift checks."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bridge.core import BridgeError, Settings, parse_request
from bridge.repository import digest
from test_bridge import request
from test_repository import REPO, fixture

ROOT = Path(__file__).resolve().parents[1]


class RequiredValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.git, self.settings, self.service, _ = fixture()
        self.policy = self.settings.repositories[REPO]
        self.policy.update(candidate_only=True, write_denied_paths=['validation.json', 'protected'],
                           required_validation={'manifest': 'validation.json', 'profile': 'tests'})
        self.candidate = {'base_commit': self.git.head, 'changes': [{'path': 'src/calc.py',
            'operation': 'update', 'expected_sha256': digest(self.git.files['src/calc.py']),
            'content': 'def add(a, b):\n    return a + b\n'}]}

    async def receipts(self):
        inspected = await self.service.evaluate(REPO, 'main', self.candidate, 'inspect')
        self.candidate['fingerprint'] = inspected['candidate_fingerprint']
        validated = await self.service.evaluate(REPO, 'main', self.candidate, 'validate', 'validation.json', 'tests')
        self.assertTrue(validated['passed'])
        return inspected['inspection_receipt'], validated['validation_receipt']

    async def test_required_loop_commits_and_readback_is_exact(self):
        receipts = await self.receipts()
        result = await self.service.persist(REPO, 'main', self.candidate, 'Fix arithmetic', *receipts)
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.git.files['src/calc.py'], self.candidate['changes'][0]['content'].encode())

    async def test_protected_controls_rejected_for_create_update_delete(self):
        for path, operation, content in [('validation.json', 'update', '{}'),
                                          ('validation.json', 'delete', None),
                                          ('protected/guard.py', 'create', 'pass\n')]:
            change = {'path': path, 'operation': operation,
                      'expected_sha256': digest(self.git.files[path]) if path in self.git.files else None}
            if content is not None:
                change['content'] = content
            with self.assertRaisesRegex(BridgeError, 'write_path_not_allowed'):
                await self.service.evaluate(REPO, 'main', {'base_commit':self.git.head, 'changes':[change]}, 'inspect')
        self.assertEqual(self.git.writes, [])

    async def test_alternative_profile_or_manifest_cannot_issue_validation_receipt(self):
        for manifest, profile in [('other.json', 'tests'), ('validation.json', 'skip')]:
            with self.assertRaisesRegex(BridgeError, 'required_validation_mismatch'):
                await self.service.evaluate(REPO, 'main', self.candidate, 'validate', manifest, profile)
        self.assertEqual(self.git.writes, [])

    async def test_old_validation_cannot_bypass_new_policy(self):
        receipts = await self.receipts()
        self.policy['write_denied_paths'].append('new-control')
        with self.assertRaisesRegex(BridgeError, 'required_validation_mismatch'):
            await self.service.persist(REPO, 'main', self.candidate, 'stale policy', *receipts)
        self.assertEqual(self.git.writes, [])

    async def test_direct_writer_is_denied_before_program_or_network(self):
        raw = request(repository=REPO, program='src/calc.py', write={
            'message':'bypass', 'expected_commit':self.git.head})
        with self.assertRaisesRegex(BridgeError, 'candidate_required'):
            parse_request(raw, 'Bearer ' + self.settings.key, self.settings)
        self.assertEqual(self.git.calls, [])

    async def test_validation_permission_does_not_widen_trusted_program_execution(self):
        self.policy['validation_prefixes'] = ['src', 'tests', 'other']
        self.candidate['changes'].append({'path':'other/new.py','operation':'create',
            'expected_sha256':None,'content':'def run(root, request): return {}\n'})
        await self.receipts()
        with self.assertRaisesRegex(BridgeError, 'program_not_allowed'):
            parse_request(request(repository=REPO, program='other/new.py'),
                          'Bearer ' + self.settings.key, self.settings)

    async def test_invalid_host_policy_fails_closed(self):
        for delta in [{'candidate_only':False}, {'write_denied_paths':['other']},
                      {'validation_prefixes':[]}, {'required_validation':{'manifest':'../bad','profile':'tests'}}]:
            policy = {**deepcopy(self.policy), **delta}
            with self.assertRaises(BridgeError):
                Settings('k'*40,{REPO:policy},'host-secret')


class RepositoryScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')[:-1]
        cls.files = {name:(ROOT/name).read_bytes() for name in set(names)}

    def validate(self, changes):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, content in {**self.files, **changes}.items():
                target = root/path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            result = subprocess.run([sys.executable,'-I','maintenance/check_scope.py'], cwd=root,
                capture_output=True,text=True,timeout=20,env={})
            return result.returncode, json.loads(result.stdout)

    def test_existing_repository_and_safe_source_edit_pass(self):
        code = self.files['bridge/repository_query.py'] + b'\n# Source-bound query receipts remain inspectable.\n'
        status, result = self.validate({'bridge/repository_query.py':code})
        self.assertEqual(status,0,result)

    def test_framework_dependency_vendor_and_new_top_level_are_rejected(self):
        cases = [ {'bridge/unrelated.py':b'import nuxt\n'},
                  {'tests/component.vue':b'<template />'},
                  {'frontend/nuxt.config.ts':b'export default {}'},
                  {'tests/package.json':b'{"dependencies":{"vue":"latest"}}'},
                  {'tests/vendor/copied.py':b'x=1\n'},
                  {'pyproject.toml':self.files['pyproject.toml'].replace(b'dependencies = [',b'dependencies = ["numpy",',1)} ]
        for changes in cases:
            with self.subTest(paths=list(changes)):
                status, result = self.validate(changes)
                self.assertNotEqual(status,0,result)
                self.assertFalse(result['passed'])

    def test_generated_file_volume_and_stdlib_shadowing_are_rejected(self):
        for changes in [ {f'tests/generated_{i}.py':b'x=1\n' for i in range(256)},
                         {'tomllib.py':b'raise SystemExit(0)\n'},
                         {'tests/compiled.py':b'# automatically generated\nx=1\n'} ]:
            status, result = self.validate(changes)
            self.assertNotEqual(status,0,result)

    def test_bootstrap_policy_locks_guard_and_preserves_full_source_read(self):
        policy = json.loads((ROOT/'maintenance/repository-policy.json').read_text())
        Settings('k'*40, {REPO:policy}, 'host-secret')
        self.assertTrue(policy['read_all'])
        self.assertEqual(policy['write_all_refs'], ['main'])
        self.assertTrue(policy['candidate_only'])
        self.assertIn('maintenance',policy['write_denied_paths'])
        for control in ('bridge/mcp_server.py', 'bridge/github_coordination.py',
                        'bridge/github_service.py', 'bridge/http.py', 'bridge/gmail.py',
                        'bridge/google_journal.py', 'bridge/google_services.py',
                        'bridge/google_discovery', 'bridge/google_documents.py', 'bridge/google_local.py'):
            self.assertIn(control,policy['write_denied_paths'])
        self.assertIn('bridge',policy['validation_prefixes'])
        self.assertNotIn('bridge',policy['program_prefixes'])


if __name__ == '__main__':
    unittest.main()
