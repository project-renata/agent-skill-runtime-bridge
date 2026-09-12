"""Operator deployment check; retired grants never enter the generic runtime.

Vercel runs this before building. --rewrite accepts repository policy JSON only,
not a full environment/secret export. Historical reads remain available.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

REPOSITORY = 'project-renata/project-renata'
RETIRED_PATH = 'runtime-workspace'
RETIRED_REFS = {'runtime-bridge/web-workspace', 'runtime-bridge/validation-20260905'}
PATH_FIELDS = ('program_prefixes', 'data_prefixes', 'validation_prefixes', 'write_prefixes')
REF_FIELDS = ('additional_refs', 'write_refs', 'write_all_refs')


def retired_path(path):
    return path == RETIRED_PATH or path.startswith(RETIRED_PATH + '/')


def retire(repositories):
    updated = deepcopy(repositories)
    grant = updated.get(REPOSITORY)
    if grant is None:
        return updated
    if grant['ref'] in RETIRED_REFS:
        raise ValueError('retired default ref requires operator reconciliation')
    for name in ('authoring', 'repo_files'):
        grant.pop(name, None)
    for name in PATH_FIELDS:
        if name in grant:
            grant[name] = [path for path in grant[name] if not retired_path(path)]
            if not grant[name] and name != 'program_prefixes':
                del grant[name]
    for name in REF_FIELDS:
        if name in grant:
            grant[name] = [ref for ref in grant[name] if ref not in RETIRED_REFS]
            if not grant[name]:
                del grant[name]
    if 'write_prefixes_by_ref' in grant:
        grant['write_prefixes_by_ref'] = {ref: [p for p in paths if not retired_path(p)]
            for ref, paths in grant['write_prefixes_by_ref'].items() if ref not in RETIRED_REFS}
        grant['write_prefixes_by_ref'] = {ref: paths for ref, paths in grant['write_prefixes_by_ref'].items() if paths}
        if not grant['write_prefixes_by_ref']:
            del grant['write_prefixes_by_ref']
    denied = grant.setdefault('write_denied_paths', [])
    if RETIRED_PATH not in denied:
        denied.append(RETIRED_PATH)
    return updated


def validate(repositories):
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError('repository policy is required')
    if retire(repositories) != repositories:
        raise ValueError('retired repository grants or missing write denial')
    grant = repositories.get(REPOSITORY)
    if grant is not None and not grant.get('program_prefixes'):
        raise ValueError('canonical program prefix is required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rewrite', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.rewrite:
            if not args.output:
                raise ValueError('output path is required')
            repositories = retire(json.loads(args.rewrite.read_text()))
            validate(repositories)
            args.output.write_text(json.dumps(repositories, indent=2) + '\n')
        else:
            validate(json.loads(os.environ['BRIDGE_REPOSITORIES']))
    except (KeyError, ValueError, TypeError, AttributeError):
        # Never include environment values or provider credentials in build logs.
        raise SystemExit('Deployment repository policy rejected; run the operator retirement migration.') from None
    print('Deployment repository policy verified: retired grants absent; write denial enforced.')
