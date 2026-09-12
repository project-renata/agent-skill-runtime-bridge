"""Revision-bound repository queries and stateless candidates; no application policy."""
import base64
from difflib import unified_diff
import fnmatch
import hashlib
import hmac
import json
import time

from .core import (BridgeError, GitHub, MAX_FILE, MAX_FILES, MAX_TOTAL, immutable_ref,
                   readable, readable_ref, safe_path, under, writable)

MAX_INPUT = 768 * 1024
MAX_OUTPUT = 256 * 1024
MAX_SCAN_FILES = 512
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 32768
RECEIPT = 'Bridge-Candidate-SHA256: '


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise BridgeError('invalid_repository_request')


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BridgeError('invalid_repository_limit')
    return value


def text(content):
    try:
        result = content.decode('utf-8')
        if '\0' in result:
            raise UnicodeError()
        return result
    except UnicodeError:
        raise BridgeError('text_file_required', 422) from None


class Evidence:
    """Stateless signed observations, never a substitute for caller authorization."""
    def __init__(self, key):
        if not isinstance(key, str) or len(key) < 32:
            raise BridgeError('evidence_key_unavailable', 503)
        self.key = hashlib.sha256(('repository-evidence-v1:' + key).encode()).digest()

    def issue(self, kind, candidate, **facts):
        data = base64.urlsafe_b64encode(encoded({'kind': kind, 'candidate': candidate,
            'expires': int(time.time()) + 86400, **facts})).decode().rstrip('=')
        return data + '.' + hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()

    def verify(self, token, kind, candidate):
        try:
            data, signature = token.split('.')
            expected = hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
            if value['kind'] != kind or value['candidate'] != candidate or value['expires'] < time.time():
                raise ValueError()
            return value
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BridgeError('candidate_evidence_mismatch', 409) from None


class RepositoryService:
    def __init__(self, settings, *, fetch, send=None, journal=None, evidence=None, validator=None, archive=None):
        self.settings, self.fetch, self.send = settings, fetch, send
        self.archive = archive
        self.journal, self.evidence, self.validator = journal, evidence, validator

    def policy(self, repository, ref, value):
        if len(encoded(value)) > MAX_INPUT:
            raise BridgeError('repository_request_too_large', 413)
        policy = self.settings.repositories.get(repository)
        if policy is None or not readable_ref(ref, policy):
            raise BridgeError('repository_or_ref_not_allowed', 403)
        return policy

    def client(self):
        return GitHub(self.fetch, self.settings.github_token)

    async def source(self, repository, ref, value):
        policy = self.policy(repository, ref, value)
        github = self.client()
        commit = await github.resolve(repository, ref)
        return github, policy, commit

    async def content(self, github, policy, path, missing=False):
        safe_path(path)
        if not readable(path, policy):
            raise BridgeError('read_path_not_allowed', 403)
        entry = await github.entry(path)
        if entry is None:
            if missing:
                return None
            raise BridgeError('repository_entry_not_found', 404)
        return await github.blob(entry)

    async def inventory(self, github, policy, prefix, max_depth, max_entries):
        if prefix:
            safe_path(prefix)
            entry = await github.entry(prefix)
            if entry is None:
                raise BridgeError('repository_entry_not_found', 404)
            if entry.get('type') != 'tree' or entry.get('mode') != '040000':
                raise BridgeError('directory_required', 422)
            initial = entry['sha']
        else:
            initial = github.root_tree
        queue, items, complete, visited = [(prefix, initial, 1)], [], True, 0
        while queue:
            directory, sha, depth = queue.pop(0)
            for name, entry in sorted((await github.tree(sha)).items()):
                path = safe_path(directory + '/' + name if directory else name)
                visited += 1
                if visited > MAX_ENTRIES:
                    return items, False
                visible = readable(path, policy) or any(p.startswith(path + '/') for p in
                    policy['program_prefixes'] + policy.get('data_prefixes', []))
                if not visible:
                    continue
                if len(items) >= max_entries:
                    return items, False
                kind = ('directory' if entry.get('type') == 'tree' and entry.get('mode') == '040000'
                        else 'file' if entry.get('type') == 'blob' and entry.get('mode') in ('100644', '100755')
                        else 'symlink' if entry.get('mode') == '120000' else 'unsupported')
                items.append({'path': path, 'type': kind, 'size': entry.get('size'), 'git_sha': entry.get('sha')})
                if kind == 'directory':
                    if depth < max_depth:
                        queue.append((path, entry['sha'], depth + 1))
                    else:
                        complete = False
        return items, complete

    async def query(self, repository, ref, query):
        from .repository_query import query as execute_query
        return await execute_query(self, repository, ref, query)

    async def candidate(self, repository, ref, candidate):
        fields(candidate, ['base_commit', 'changes'], ['fingerprint'])
        policy = self.policy(repository, ref, candidate)
        base = candidate['base_commit']
        if not immutable_ref(base) or ref not in policy.get('write_refs', []) or immutable_ref(ref):
            raise BridgeError('candidate_revision_not_allowed', 403)
        changes = candidate['changes']
        if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_FILES:
            raise BridgeError('invalid_candidate_changes')
        github = self.client()
        await github.resolve(repository, base)
        before, after, summary = {}, {}, []
        for change in sorted(changes, key=lambda c: c.get('path', '') if isinstance(c, dict) else ''):
            fields(change, ['path', 'expected_sha256', 'operation'], ['content', 'edits'])
            path, operation = safe_path(change['path']), change['operation']
            if path in after:
                raise BridgeError('duplicate_candidate_path')
            if any(path.startswith(p + '/') or p.startswith(path + '/') for p in after):
                raise BridgeError('overlapping_candidate_paths')
            if not writable(path, policy, ref):
                raise BridgeError('write_path_not_allowed', 403)
            previous = await self.content(github, policy, path, missing=True)
            expected = change['expected_sha256']
            if expected != (digest(previous) if previous is not None else None):
                raise BridgeError('file_precondition_failed', 409)
            if operation == 'create':
                if previous is not None:
                    raise BridgeError('create_existing', 409)
            elif operation in ('update', 'delete'):
                if previous is None:
                    raise BridgeError('target_missing', 409)
            else:
                raise BridgeError('invalid_candidate_operation')
            if operation == 'delete':
                if 'content' in change or 'edits' in change:
                    raise BridgeError('invalid_delete')
                value = None
            elif ('content' in change) == ('edits' in change):
                raise BridgeError('exactly_one_edit_representation_required')
            elif 'content' in change:
                if not isinstance(change['content'], str):
                    raise BridgeError('text_file_required')
                value = change['content'].encode()
            else:
                if previous is None:
                    raise BridgeError('patch_requires_existing_file')
                source = text(previous)
                edits = change['edits']
                if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
                    raise BridgeError('invalid_patch')
                cursor, parts = 0, []
                for edit in edits:
                    fields(edit, ['start', 'end', 'expected', 'replacement'])
                    start = integer(edit['start'], cursor, len(source))
                    end = integer(edit['end'], start, len(source))
                    if not isinstance(edit['replacement'], str) or source[start:end] != edit['expected']:
                        raise BridgeError('patch_mismatch', 409)
                    parts.extend([source[cursor:start], edit['replacement']]); cursor = end
                value = ''.join(parts + [source[cursor:]]).encode()
            if value is not None and (len(value) > MAX_FILE or b'\0' in value):
                raise BridgeError('candidate_file_limit', 413)
            if value == previous:
                raise BridgeError('candidate_no_change')
            if previous is not None:
                before[path] = previous
            after[path] = value
            summary.append({'path': path, 'operation': operation,
                'before_sha256': digest(previous) if previous is not None else None,
                'after_sha256': digest(value) if value is not None else None})
        if sum(len(v) for v in after.values() if v is not None) > MAX_TOTAL:
            raise BridgeError('changes_too_large', 413)
        fingerprint = digest(encoded({'protocol': 1, 'repository': repository, 'ref': ref,
                                     'base_commit': base, 'changes': summary}))
        if candidate.get('fingerprint', fingerprint) != fingerprint:
            raise BridgeError('candidate_fingerprint_mismatch', 409)
        return github, policy, base, before, after, summary, fingerprint

    async def evaluate(self, repository, ref, candidate, operation, manifest=None, profile=None, max_bytes=65536):
        github, policy, base, before, after, summary, fingerprint = await self.candidate(repository, ref, candidate)
        result = {'repository': repository, 'resolved_commit': base, 'candidate_fingerprint': fingerprint,
                  'operation': operation, 'changed_files': summary}
        if operation == 'inspect':
            limit = integer(max_bytes, 1024, MAX_OUTPUT)
            diffs = []
            for path in after:
                # splitlines with explicit no-newline records preserves exact EOF changes.
                lines = list(unified_diff(text(before.get(path, b'')).splitlines(keepends=True),
                    text(after[path] or b'').splitlines(keepends=True),
                    fromfile='a/' + path if path in before else '/dev/null',
                    tofile='b/' + path if after[path] is not None else '/dev/null'))
                diffs.append(''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines))
            raw = ''.join(diffs).encode()
            complete = len(raw) + len(encoded(result)) + 2048 <= limit
            result.update(diff=raw[:max(0, limit - len(encoded(result)) - 2048)].decode('utf-8', errors='ignore'),
                          complete=complete, truncated=not complete)
            if complete and self.evidence:
                result['inspection_receipt'] = self.evidence.issue('inspection', fingerprint, complete=True)
            return result
        if operation != 'validate' or self.validator is None:
            raise BridgeError('validation_unavailable', 503)
        safe_path(manifest)
        raw = after.get(manifest) if manifest in after else await self.content(github, policy, manifest)
        if raw is None:
            raise BridgeError('validation_manifest_deleted')
        try:
            contract = json.loads(raw)
        except (ValueError, TypeError):
            raise BridgeError('invalid_validation_manifest') from None
        from .validation import validation_profile
        selected = validation_profile(contract, profile)
        files = {manifest: raw}
        for selector in selected['files']:
            if selector.endswith('/') or selector == '':
                prefix = selector[:-1] if selector else ''
                if prefix and await github.entry(prefix) is None and any(
                        path.startswith(prefix + '/') and value is not None for path, value in after.items()):
                    entries, complete = [], True
                else:
                    entries, complete = await self.inventory(github, policy, prefix, 128, MAX_SCAN_FILES)
                if not complete:
                    raise BridgeError('validation_snapshot_incomplete', 413)
                for item in entries:
                    if item['type'] not in ('file', 'directory'):
                        raise BridgeError('unsupported_repository_entry', 422)
                    if item['type'] == 'file':
                        files[item['path']] = await self.content(github, policy, item['path'])
            else:
                safe_path(selector)
                if selector in after:
                    if after[selector] is None:
                        raise BridgeError('validation_selector_deleted')
                    files[selector] = after[selector]
                else:
                    files[selector] = await self.content(github, policy, selector)
        files.update(before)
        for path, content in after.items():
            if content is None:
                files.pop(path, None)
            else:
                files[path] = content
        if len(files) > MAX_SCAN_FILES or sum(map(len, files.values())) > MAX_SCAN_BYTES:
            raise BridgeError('validation_snapshot_limit', 413)
        for path in files:
            if path.endswith('.py') and not under(path, policy['program_prefixes']):
                raise BridgeError('validation_code_path_not_allowed', 403)
        validation = await self.validator(files, selected)
        result.update(validation)
        result['manifest_sha256'] = digest(raw)
        result['profile'] = profile
        if result.get('passed') is True and self.evidence:
            result['validation_receipt'] = self.evidence.issue('validation', fingerprint,
                manifest_sha256=digest(raw), profile=profile, passed=True,
                environment=validation.get('environment', {}),
                snapshot_sha256=digest(encoded({p: digest(v) for p, v in sorted(files.items())})))
        return result

    async def persist(self, repository, ref, candidate, message, inspection_receipt, validation_receipt):
        github, policy, base, before, after, summary, fingerprint = await self.candidate(repository, ref, candidate)
        if 'fingerprint' not in candidate:
            raise BridgeError('candidate_fingerprint_required')
        if self.evidence is None or self.journal is None:
            raise BridgeError('candidate_persistence_unavailable', 503)
        inspection = self.evidence.verify(inspection_receipt, 'inspection', fingerprint)
        validation = self.evidence.verify(validation_receipt, 'validation', fingerprint)
        if inspection.get('complete') is not True or validation.get('passed') is not True:
            raise BridgeError('candidate_evidence_incomplete', 409)
        if not isinstance(message, str) or not message.strip() or len(message) > 500 or RECEIPT in message:
            raise BridgeError('invalid_commit_message')
        bound_message = message.rstrip() + '\n\n' + RECEIPT + fingerprint
        key = digest(encoded({'repository': repository, 'ref': ref, 'candidate': fingerprint, 'message': bound_message}))

        async def reconcile():
            current = self.client()
            sha = await current.resolve(repository, ref)
            branch_head = sha
            for _ in range(32):
                if sha == base:
                    return None, branch_head
                commit = await current.get(f'/repos/{repository}/git/commits/{sha}')
                parents = commit.get('parents', [])
                if (commit.get('message', '').rstrip() == bound_message
                        and [p.get('sha') for p in parents] == [base]):
                    # Verify every changed byte, and that no additional changes were committed.
                    comparison = await current.get(f'/repos/{repository}/compare/{base}...{sha}')
                    paths = comparison.get('files', [])
                    if len(paths) >= 300 or {f.get('filename') for f in paths} != set(after):
                        raise BridgeError('commit_readback_mismatch', 409)
                    await current.resolve(repository, sha)
                    for path, expected in after.items():
                        actual = await self.content(current, policy, path, missing=True)
                        if actual != expected:
                            raise BridgeError('commit_readback_mismatch', 409)
                    return {'repository': repository, 'resolved_commit': base, 'candidate_fingerprint': fingerprint,
                        'operation': 'commit', 'commit': sha, 'changed_files': summary,
                        'readback_verified': True, 'complete': True}, sha
                if len(parents) != 1:
                    break
                sha = parents[0]['sha']
            return None, None

        receipt, head = await reconcile()
        if receipt:
            return {**receipt, 'replayed': True}
        if head != base:
            raise BridgeError('branch_conflict', 409)
        claimed = await self.journal.claim('candidate:' + key, fingerprint)
        if not claimed:
            raise BridgeError('commit_pending_or_indeterminate', 409)
        request = {'ref': ref, 'write': {'message': bound_message}}
        await github.commit_changes(request, base, before, after, policy, self.send)
        receipt, _ = await reconcile()
        if not receipt:
            raise BridgeError('commit_readback_mismatch', 409)
        return {**receipt, 'replayed': False}
