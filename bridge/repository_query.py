"""Efficient bounded reads with immutable, signed search continuation."""
import fnmatch
from bisect import bisect_left

from .core import BridgeError, MAX_FILE, MAX_TREE_ENTRIES, immutable_ref, readable, safe_path
from .repository import (Evidence, MAX_ENTRIES, MAX_OUTPUT, MAX_SCAN_BYTES,
                         MAX_SCAN_FILES, digest, encoded, fields, integer, text)
from .request_budget import request_budget

MAX_QUERY_REQUESTS = 32
ARCHIVE_MIN_FILES = 8


async def inventory(github, policy, prefix):
    """One scoped recursive listing, never a request for every directory."""
    tree = github.root_tree
    if prefix:
        safe_path(prefix)
        entry = await github.entry(prefix)
        if entry is None:
            raise BridgeError('repository_entry_not_found', 404)
        if (entry.get('type'), entry.get('mode')) != ('tree', '040000'):
            raise BridgeError('directory_required', 422)
        tree = entry['sha']
    listing = await github.get(f'/repos/{github.repo}/git/trees/{tree}?recursive=1')
    raw = listing.get('tree')
    if not isinstance(raw, list) or len(raw) > MAX_TREE_ENTRIES:
        raise BridgeError('invalid_upstream_response', 502)
    entries, seen, blocked = [], set(), set()
    for entry in sorted(raw, key=lambda e: e.get('path', '') if isinstance(e, dict) else ''):
        if not isinstance(entry, dict):
            raise BridgeError('invalid_upstream_response', 502)
        relative = safe_path(entry.get('path'))
        path = prefix + '/' + relative if prefix else relative
        if path in seen or not immutable_ref(entry.get('sha')):
            raise BridgeError('invalid_upstream_response', 502)
        seen.add(path)
        if any('/'.join(path.split('/')[:i]) in blocked for i in range(1, len(path.split('/')))):
            continue
        kind = ('directory' if (entry.get('type'), entry.get('mode')) == ('tree', '040000')
                else 'file' if entry.get('type') == 'blob' and entry.get('mode') in ('100644', '100755')
                else 'symlink' if entry.get('mode') == '120000' else 'unsupported')
        if kind != 'directory':
            blocked.add(path)
        if kind == 'file' and (type(entry.get('size')) is not int or entry['size'] < 0):
            raise BridgeError('invalid_upstream_response', 502)
        entries.append({'path': path, 'relative': relative, 'type': kind,
                        'size': entry.get('size'), 'git_sha': entry['sha'], 'entry': entry})
    return entries, tree, not bool(listing.get('truncated'))


def visible(path, policy):
    return readable(path, policy) or any(p.startswith(path + '/') for p in
        policy['program_prefixes'] + policy.get('data_prefixes', []))


async def read(service, github, policy, query, result, limit):
    path = query.get('path')
    content = await service.content(github, policy, path)
    source = text(content)
    if ('start_line' in query or 'end_line' in query) and ('byte_start' in query or 'byte_count' in query):
        raise BridgeError('ambiguous_read_range')
    if 'byte_start' in query or 'byte_count' in query:
        start = integer(query.get('byte_start', 0), 0, len(content))
        count = integer(query.get('byte_count', limit), 1, MAX_FILE)
        source = text(content[start:start + count])
        requested = {'byte_start': start, 'byte_count': count}
        whole = start == 0 and len(source.encode()) == len(content)
    else:
        lines = source.splitlines(keepends=True)
        start = integer(query.get('start_line', 1), 1, max(1, len(lines) + 1))
        end = integer(query.get('end_line', max(1, len(lines))), start, max(start, len(lines)))
        source = ''.join(lines[start - 1:end])
        requested = {'start_line': start, 'end_line': end}
        whole = start == 1 and end >= len(lines)
    raw = source.encode()
    shown = raw[:limit - 768].decode('utf-8', errors='ignore')
    result.update(path=path, sha256=digest(content), size=len(content), requested_range=requested,
        text=shown, truncated=len(raw) > limit - 768, complete=len(raw) <= limit - 768,
        whole_file=whole and len(raw) <= limit - 768, returned_bytes=len(shown.encode()))
    return result


async def query(service, repository, ref, value):
    with request_budget(MAX_QUERY_REQUESTS) as budget:
        result = await _query(service, repository, ref, value)
    result['read_diagnostics'] = {'upstream_requests': budget.used,
                                'request_limit': budget.limit}
    return result


async def _query(service, repository, ref, query):
    fields(query, ['operation'], ['path', 'prefix', 'max_depth', 'max_entries', 'max_bytes',
        'pattern', 'glob', 'suffix', 'max_results', 'start_line', 'end_line',
        'byte_start', 'byte_count', 'cursor'])
    operation = query['operation']
    if operation not in ('tree', 'search', 'read'):
        raise BridgeError('invalid_query_operation')
    policy = service.policy(repository, ref, query)
    limit = integer(query.get('max_bytes', 32768), 1024, MAX_OUTPUT)
    prefix = query.get('prefix', '')
    if not isinstance(prefix, str):
        raise BridgeError('invalid_path')
    if prefix:
        safe_path(prefix)
    pattern, glob, suffix = query.get('pattern'), query.get('glob', '*'), query.get('suffix', '')
    if operation == 'search' and (not isinstance(pattern, str) or not 1 <= len(pattern) <= 256 or '\n' in pattern):
        raise BridgeError('invalid_literal_pattern')
    if not isinstance(glob, str) or len(glob) > 256 or not isinstance(suffix, str) or len(suffix) > 128:
        raise BridgeError('invalid_path_filter')
    max_results = integer(query.get('max_results', 100), 1, 1000)
    depth = integer(query.get('max_depth', 64), 1, 128)
    count = integer(query.get('max_entries', MAX_ENTRIES), 1, MAX_ENTRIES)
    binding = digest(encoded({'repository': repository, 'ref': ref, 'policy': policy,
                              'query': {k: v for k, v in query.items() if k != 'cursor'}}))
    evidence = service.evidence or Evidence(service.settings.key)
    start, line_start, source_ref, prior_skipped = 0, 0, ref, 0
    if query.get('cursor') is not None:
        if operation != 'search':
            raise BridgeError('invalid_search_cursor')
        cursor = evidence.verify(query['cursor'], 'repository-search', binding)
        source_ref = cursor.get('commit')
        if not immutable_ref(source_ref):
            raise BridgeError('invalid_search_cursor')
        start = integer(cursor.get('index'), 0, MAX_TREE_ENTRIES)
        line_start = integer(cursor.get('line'), 0, MAX_FILE)
        prior_skipped = integer(cursor.get('skipped', 0), 0, MAX_TREE_ENTRIES)
    github = service.client()
    commit = await github.resolve(repository, source_ref)
    result = {'repository': repository, 'resolved_commit': commit, 'operation': operation}
    if operation == 'read':
        return await read(service, github, policy, query, result, limit)
    entries, root_tree, listing_complete = await inventory(github, policy, prefix)
    if operation == 'tree':
        items, complete = [], listing_complete
        for item in entries:
            if not visible(item['path'], policy):
                continue
            shown = {k: item[k] for k in ('path', 'type', 'size', 'git_sha')}
            if item['relative'].count('/') + 1 > depth:
                complete = False
                continue
            if len(items) >= count or len(encoded(items + [shown])) > limit - 768:
                complete = False
                break
            items.append(shown)
        result.update(items=items, complete=complete, truncated=not complete,
                      scanned_files=0, scanned_bytes=0, skipped_count=0)
        return result

    files = [e for e in entries if e['type'] == 'file' and readable(e['path'], policy)
             and fnmatch.fnmatchcase(e['path'], glob) and e['path'].endswith(suffix)]
    if start > len(files):
        raise BridgeError('invalid_search_cursor')
    # Plan only this page. Whole-subtree archives are used only when the entire
    # subtree fits the byte bound and is readable; no hidden whole-repo download.
    selected, planned_bytes = {}, 0
    for item in files[start:start + MAX_SCAN_FILES]:
        if item['size'] > MAX_FILE:
            continue
        if planned_bytes + item['size'] > MAX_SCAN_BYTES:
            break
        selected[item['path']] = item
        planned_bytes += item['size']
    groups = {}
    if service.archive is not None and listing_complete:
        all_files = [e for e in entries if e['type'] == 'file']
        paths = [e['path'] for e in all_files]
        byte_sums, denied_sums = [0], [0]
        for item in all_files:
            byte_sums.append(byte_sums[-1] + item['size'])
            denied_sums.append(denied_sums[-1] + (not readable(item['path'], policy)))
        selected_paths = sorted(selected)
        directories = [(prefix, root_tree)] + [(e['path'], e['git_sha']) for e in entries if e['type'] == 'directory']
        for directory, sha in sorted(directories, key=lambda d: (d[0].count('/'), d[0])):
            stem = directory + '/' if directory else ''
            # Prefix sums keep planning O(entries log entries), including wide
            # repositories. Filtering must not introduce a CPU quadratic scan.
            upper = stem[:-1] + '0' if stem else None
            lo, hi = bisect_left(paths, stem), bisect_left(paths, upper) if upper else len(paths)
            left = bisect_left(selected_paths, stem)
            right = bisect_left(selected_paths, upper) if upper else len(selected_paths)
            if (right - left < ARCHIVE_MIN_FILES or byte_sums[hi] - byte_sums[lo] > MAX_SCAN_BYTES
                    or denied_sums[hi] != denied_sums[lo]):
                continue
            wanted = {p: selected[p] for p in selected_paths[left:right] if p not in groups}
            if len(wanted) < ARCHIVE_MIN_FILES:
                continue
            group = (stem, sha, wanted)
            for path in wanted:
                groups[path] = group
    loaded = {}
    items, scanned, size, skipped = [], 0, 0, []
    index, line, reason = start, line_start, None
    while index < len(files):
        item = files[index]
        if item['size'] > MAX_FILE:
            skipped.append(item['path']); index += 1; line = 0
            continue
        if scanned >= MAX_SCAN_FILES or size + item['size'] > MAX_SCAN_BYTES:
            reason = 'scan_budget'; break
        try:
            if item['path'] not in loaded:
                group = groups.get(item['path'])
                if group is not None:
                    stem, sha, wanted = group
                    manifest = {p[len(stem):]: e['entry'] for p, e in wanted.items()}
                    archive = await service.archive(repository, sha, github.headers, manifest)
                    if set(archive) != set(manifest):
                        raise BridgeError('invalid_upstream_response', 502)
                    for relative, content in archive.items():
                        loaded[stem + relative] = github.verify_blob(content, manifest[relative], MAX_FILE)
                else:
                    loaded[item['path']] = await github.blob(item['entry'])
            content = loaded[item['path']]
        except BridgeError as error:
            if error.code not in ('github_request_budget_exceeded', 'github_transport_busy',
                                  'github_rate_limited', 'github_coordination_unavailable'):
                raise
            reason = 'request_budget' if error.code == 'github_request_budget_exceeded' else error.code
            if 'retry_after' in error.details:
                result['retry_after'] = error.details['retry_after']
            if 'reset_at' in error.details:
                result['reset_at'] = error.details['reset_at']
            break
        scanned += 1; size += len(content)
        try:
            lines = text(content).splitlines()
        except BridgeError:
            skipped.append(item['path']); index += 1; line = 0
            continue
        while line < len(lines):
            raw = lines[line]
            at = raw.find(pattern)
            if at >= 0:
                snippet = raw[max(0, at - 80):at + len(pattern) + 160]
                match = {'path': item['path'], 'line': line + 1, 'snippet': snippet,
                         'snippet_truncated': snippet != raw}
                if len(encoded(items + [match])) > limit - 1024:
                    if not items:
                        raise BridgeError('query_output_limit_too_small', 413)
                    reason = 'output_budget'; break
                items.append(match)
            line += 1
            if len(items) >= max_results:
                reason = 'result_limit'; break
        if line == len(lines):
            index += 1; line = 0
        if reason:
            break
    more = index < len(files)
    skipped_total = prior_skipped + len(skipped)
    cursor = evidence.issue('repository-search', binding, commit=commit, index=index, line=line,
                            skipped=skipped_total) if more else None
    complete = not more and listing_complete and not skipped_total
    result.update(items=items, complete=complete, truncated=not complete,
        scanned_files=scanned, scanned_bytes=size, skipped_count=len(skipped),
        total_skipped_count=skipped_total, next_cursor=cursor,
        stop_reason=(reason if more else 'upstream_tree_truncated' if not listing_complete
                     else 'skipped_files' if skipped_total else None))
    if not listing_complete:
        result['narrower_prefix_required'] = True
    return result
