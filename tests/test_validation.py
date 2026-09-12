import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge.core import BridgeError
from bridge.validation import IMAGE, execute_validation, validation_profile


def profile(**changes):
    return validation_profile({'version': 1, 'profiles': {'test': {
        'files': ['test.py'], 'commands': [{'executable': 'python', 'argv': ['test.py']}], **changes}}}, 'test')


class ValidationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_contract_credential_absence_and_cleanup(self):
        observations = {'writes': {}}
        class Batch:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def write_bytes(self, path, value): observations['writes'][path] = value
            def write_text(self, path, value): observations['writes'][path] = value.encode()
        class Box:
            image = IMAGE
            fs = SimpleNamespace(batch=Batch)
            async def __aenter__(self): return self
            async def __aexit__(self, *_): observations['destroyed'] = True
            async def run_process(self, executable, argv, **kwargs):
                observations['command'] = executable, argv, kwargs
                return SimpleNamespace(returncode=0, stdout=json.dumps({'passed': True, 'commands': [], 'complete': True}))
        def create(**kwargs):
            observations['options'] = kwargs
            return Box()
        with patch.dict('os.environ', {'BRIDGE_GITHUB_TOKEN': 'host-secret', 'BRIDGE_GOOGLE_CREDENTIALS': 'google-secret'}):
            with patch('vercel.sandbox.create_sandbox', create):
                result = await execute_validation({'test.py': b'print(1)'}, profile())
        self.assertTrue(result['passed'])
        self.assertTrue(observations['destroyed'])
        self.assertEqual(observations['options']['network_policy'].mode, 'deny-all')
        self.assertFalse(observations['options']['persistent'])
        self.assertEqual(observations['options']['env'], {})
        self.assertEqual(observations['options']['ports'], [])
        self.assertEqual(observations['command'][0:2], ('python3', ['-I', '.bridge/runner.py', '.bridge/request.json']))
        self.assertNotIn(b'host-secret', b''.join(observations['writes'].values()))
        self.assertNotIn(b'google-secret', b''.join(observations['writes'].values()))

    async def test_missing_cwd_and_script_rejected_before_provider(self):
        with patch('vercel.sandbox.create_sandbox') as create:
            for p in (profile(commands=[{'executable': 'python', 'argv': ['test.py'], 'cwd': 'absent'}]),
                      profile(commands=[{'executable': 'python', 'argv': ['missing.py']}])):
                with self.assertRaises(BridgeError):
                    await execute_validation({'test.py': b'print(1)'}, p)
            create.assert_not_called()

    async def test_provider_failure_has_no_local_execution_fallback_or_secret_text(self):
        with patch('vercel.sandbox.create_sandbox', side_effect=ValueError('secret-token')):
            with self.assertRaisesRegex(BridgeError, 'validation_environment_unavailable') as error:
                await execute_validation({'test.py': b'print(1)'}, profile())
        self.assertNotIn('secret', str(error.exception))


@unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux supervisor is tested live in the pinned microVM on other hosts')
class LinuxSupervisorTests(unittest.TestCase):
    def run_source(self, source, **limits):
        runner = Path(__file__).resolve().parents[1] / 'bridge/validation_runner.py'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'repository').mkdir()
            (root / 'repository/test.py').write_text(source)
            (root / 'request.json').write_text(json.dumps(profile(**limits)))
            cmd = [sys.executable, '-I', str(runner), str(root / 'request.json')]
            import os
            if os.geteuid() != 0:
                cmd = ['sudo', '-n', *cmd]
            result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_chroot_uid_seccomp_and_environment(self):
        source = '''import os, pathlib, socket, subprocess
assert os.getuid() == 65534
assert set(os.environ) <= {'LANG','HOME','TMPDIR','PYTHONIOENCODING'}
for p in ['/etc/passwd', '/proc/self/environ', '../../etc/passwd']:
 try: pathlib.Path(p).read_bytes()
 except OSError: pass
 else: raise AssertionError(p)
for f in [lambda: socket.socket(), lambda: os.fork(), lambda: subprocess.run(['/bin/sh','-c','echo bad']), lambda: pathlib.Path('test.py').write_text('bad')]:
 try: f()
 except OSError: pass
 else: raise AssertionError('boundary escaped')
pathlib.Path('/tmp/link').symlink_to('/etc/passwd')
try: pathlib.Path('/tmp/link').read_bytes()
except OSError: pass
else: raise AssertionError('symlink escaped')
print('secure')
'''
        result = self.run_source(source)
        self.assertTrue(result['passed'], result)

    def test_timeout_including_closed_output_pipes(self):
        for source in ('while True: pass', 'import os,time\nos.close(1); os.close(2); time.sleep(100)'):
            result = self.run_source(source, timeout_seconds=1)
            self.assertFalse(result['passed'])
            self.assertTrue(result['commands'][0]['timed_out'] or result['commands'][0]['exit_status'] < 0)
            self.assertLess(result['duration_seconds'], 3)

    def test_output_cap(self):
        result = self.run_source('print("x" * 1000000)', output_bytes=1024)
        self.assertFalse(result['passed']); self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['commands'][0]['stdout'].encode()), 1024)

    def test_failure_and_memory_limit(self):
        result = self.run_source('raise AssertionError("expected failure")')
        self.assertEqual(result['commands'][0]['exit_status'], 1)
        result = self.run_source('x = bytearray(1024 * 1024 * 1024)')
        self.assertFalse(result['passed'])
