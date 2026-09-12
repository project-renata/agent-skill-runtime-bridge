"""Opt-in verification of the actual pinned isolated environment. No repository writes."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.validation import execute_validation, validation_profile

ISOLATION = '''import os, pathlib, socket, subprocess
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
print('isolation passed')
'''


async def check(report):
    cases = [('isolation', ISOLATION, {}, True),
        ('timeout', 'while True: pass', {'timeout_seconds': 1}, False),
        ('closed_pipe_timeout', 'import os,time\nos.close(1); os.close(2); time.sleep(100)', {'timeout_seconds': 1}, False),
        ('output', 'print("x" * 1000000)', {'output_bytes': 1024}, False),
        ('failed_check', 'raise SystemExit(7)', {}, False),
        ('memory', 'try:\n x=bytearray(1024*1024*1024)\nexcept MemoryError:\n print("memory bounded")\nelse:\n raise AssertionError("limit missing")', {}, True)]
    results = []
    for name, source, limits, expected in cases:
        profile = validation_profile({'version': 1, 'profiles': {'test': {
            'files': ['test.py'], 'commands': [{'executable': 'python', 'argv': ['test.py']}], **limits}}}, 'test')
        result = await execute_validation({'test.py': source.encode()}, profile)
        assert result['passed'] is expected, (name, result)
        if 'timeout' in name:
            assert result['duration_seconds'] < 3
        if name == 'output':
            assert result['truncated'] and len(result['commands'][0]['stdout'].encode()) <= 1024
        if name == 'failed_check':
            assert result['commands'][0]['exit_status'] == 7
        results.append({'case': name, 'check_passed': True, 'receipt': result})
        print(json.dumps({'case': name, 'check_passed': True}), flush=True)
    report.write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.report))
