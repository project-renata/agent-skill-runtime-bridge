"""Opt-in MCP loop on an existing allowlisted integration ref, with exact cleanup.

The operator supplies authenticated host transport and MCP call functions. This
module never discovers credentials, uses a main branch, sends messages or starts
another agent. Temporary fixture paths are verified before creation and removal.
"""
import json
from pathlib import Path
import uuid

from bridge.core import GitHub, safe_path


async def check(call, settings, fetch, send, repository, ref, fixture_prefix):
    policy = settings.repositories[repository]
    if ref == policy['ref'] or ref not in policy.get('write_refs', []):
        raise ValueError('an explicit writable integration ref is required')
    prefix = safe_path(fixture_prefix) + '/validation-' + uuid.uuid4().hex[:12]
    github = GitHub(fetch, settings.github_token)
    base = await github.resolve(repository, ref)
    if await github.entry(prefix) is not None:
        raise ValueError('fixture already exists')
    example = Path(__file__).resolve().parents[1] / 'examples/repository-validation'
    files = {prefix + '/' + name: (example / name).read_bytes()
             for name in ('calc.py', 'check_source.py', 'tests/test_calc.py')}
    files[prefix + '/calc.py'] = files[prefix + '/calc.py'].replace(b'a + b', b'a - b')
    manifest = {'version': 1, 'profiles': {'tests': {'files': [prefix + '/'], 'commands': [
        {'executable': 'python', 'argv': ['-m', 'unittest', 'discover', '-s', 'tests'], 'cwd': prefix},
        {'executable': 'python', 'argv': ['check_source.py'], 'cwd': prefix}]}}}
    files[prefix + '/validation.json'] = json.dumps(manifest).encode()
    seeded = await github.commit_changes({'ref': ref, 'write': {'message': 'Create isolated repository protocol fixture'}},
        base, {}, files, policy, send)
    report = {'repository': repository, 'ref': ref, 'prefix': prefix, 'seed_commit': seeded['commit']}
    expected_files = dict(files)
    expected_head = seeded['commit']
    try:
        search = await call('query_repository', repository=repository, ref=ref,
            query={'operation': 'search', 'prefix': prefix, 'pattern': 'a - b'})
        assert search['complete'] and search['items'][0]['path'] == prefix + '/calc.py'
        read = await call('query_repository', repository=repository, ref=ref,
            query={'operation': 'read', 'path': prefix + '/calc.py'})
        offset = read['text'].index('a - b') + 2
        candidate = {'base_commit': read['resolved_commit'], 'changes': [{'path': prefix + '/calc.py',
            'operation': 'update', 'expected_sha256': read['sha256'],
            'edits': [{'start': offset, 'end': offset + 1, 'expected': '-', 'replacement': '+'}]}]}
        inspected = await call('evaluate_repository_candidate', repository=repository, ref=ref,
            candidate=candidate, operation='inspect')
        assert inspected['complete'] and '+    return a + b' in inspected['diff']
        candidate['fingerprint'] = inspected['candidate_fingerprint']
        validated = await call('evaluate_repository_candidate', repository=repository, ref=ref,
            candidate=candidate, operation='validate', manifest=prefix + '/validation.json', profile='tests')
        assert validated['passed'] and validated['candidate_fingerprint'] == candidate['fingerprint'], validated
        assert await github.resolve(repository, ref) == seeded['commit']
        result = await call('commit_repository_candidate', repository=repository, ref=ref, candidate=candidate,
            message='Verify exact candidate persistence', inspection_receipt=inspected['inspection_receipt'],
            validation_receipt=validated['validation_receipt'])
        expected_head = result['commit']
        expected_files[prefix + '/calc.py'] = files[prefix + '/calc.py'].replace(b'a - b', b'a + b')
        again = await call('commit_repository_candidate', repository=repository, ref=ref, candidate=candidate,
            message='Verify exact candidate persistence', inspection_receipt=inspected['inspection_receipt'],
            validation_receipt=validated['validation_receipt'])
        assert again['replayed'] and again['commit'] == result['commit']
        readback = await call('query_repository', repository=repository, ref=result['commit'],
            query={'operation': 'read', 'path': prefix + '/calc.py'})
        assert readback['text'].encode() == expected_files[prefix + '/calc.py']
        assert result['candidate_fingerprint'] == validated['candidate_fingerprint']
        report.update(passed=True, candidate_fingerprint=candidate['fingerprint'], result_commit=result['commit'],
            readback_sha256=readback['sha256'], validation=validated, replay_verified=True)
    finally:
        current = await github.resolve(repository, ref, expected_commit=expected_head)
        for path, expected in expected_files.items():
            actual = await github.blob(await github.entry(safe_path(path)))
            if actual != expected:
                raise RuntimeError('fixture changed unexpectedly; cleanup refused')
        receipt = await github.commit_changes({'ref': ref, 'write': {'message': 'Remove isolated repository protocol fixture'}},
            current, expected_files, dict.fromkeys(expected_files), policy, send)
        await github.resolve(repository, ref, expected_commit=receipt['commit'])
        assert all([await github.entry(path) is None for path in expected_files])
        report['cleanup_commit'] = receipt['commit']
        report['cleanup_verified'] = True
    return report
