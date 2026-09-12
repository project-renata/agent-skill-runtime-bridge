"""Opt-in stateless candidate smoke on existing source; never seed remote files.

The operator supplies an authenticated call function and an existing source path
and validation manifest. Only query/inspect/validate are called. No credential
lookup, repository mutation, integration ref or persistent scratch code is needed.
"""


async def check(call, repository, ref, path, manifest, profile):
    if not path.endswith('.py'):
        raise ValueError('select an existing Python source file')
    read = await call('query_repository', repository=repository, ref=ref,
        query={'operation': 'read', 'path': path})
    candidate = {'base_commit': read['resolved_commit'], 'changes': [{'path': path,
        'operation': 'update', 'expected_sha256': read['sha256'],
        'edits': [{'start': 0, 'end': 0, 'expected': '',
                   'replacement': '# Disposable candidate validation; no repository write.\n'}]}]}
    inspected = await call('evaluate_repository_candidate', repository=repository, ref=ref,
        candidate=candidate, operation='inspect')
    assert inspected['complete'], inspected
    candidate['fingerprint'] = inspected['candidate_fingerprint']
    validated = await call('evaluate_repository_candidate', repository=repository, ref=ref,
        candidate=candidate, operation='validate', manifest=manifest, profile=profile)
    assert validated['passed'] and validated['candidate_fingerprint'] == candidate['fingerprint'], validated
    after = await call('query_repository', repository=repository, ref=ref,
        query={'operation': 'read', 'path': path})
    assert after['resolved_commit'] == read['resolved_commit'] and after['sha256'] == read['sha256']
    return {'repository': repository, 'ref': ref, 'path': path, 'passed': True,
        'base_commit': read['resolved_commit'], 'candidate_fingerprint': candidate['fingerprint'],
        'repository_unchanged': True, 'validation': validated}
