"""Production query + HTTP + cache against Git objects, never a live load test."""
import asyncio
import base64
import gzip
import io
import json
import tarfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import MagicMock, patch

from bridge.core import BridgeError, Settings
from bridge.http import fetch_json, fetch_query_archive
from bridge.repository import RepositoryService
from bridge.repository_models import SearchQuery
from bridge.transport_cache import TransportState
from test_repository import GitFixture, REPO


class QueryEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now, self.requests, self.archive_bytes = 1000., [], []
        self.state = TransportState(clock=lambda: self.now)
        self.git = GitFixture({f'd{i:04d}/note.md': f'needle {i}\n'.encode() for i in range(256)})
        self.settings = Settings('k' * 40, {REPO: {'ref': 'main', 'program_prefixes': ['src'],
                                                  'read_all': True}}, 'host-secret')
        self.service = RepositoryService(self.settings, fetch=fetch_json, archive=fetch_query_archive)
        opener = MagicMock()
        opener.open.side_effect = self.open_request
        for target, value in [('bridge.http._transport', self.state),
                              ('bridge.http.build_opener', MagicMock(return_value=opener)),
                              ('bridge.http.coordinator_from_env', MagicMock(return_value=None))]:
            active = patch(target, value)
            active.start(); self.addCleanup(active.stop)

    def open_request(self, request, **kwargs):
        self.assertEqual(request.get_method(), 'GET')
        self.requests.append(request.full_url)
        if '/tarball/' in request.full_url:
            sha = request.full_url.rsplit('/', 1)[-1]
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:gz') as tar:
                def members(tree, prefix=''):
                    for entry in self.git.trees[tree]['tree']:
                        name = prefix + entry['path']
                        if entry['type'] == 'tree':
                            yield from members(entry['sha'], name + '/')
                        elif entry['mode'] in ('100644', '100755'):
                            yield name, self.git.objects[entry['sha']]
                for path, content in members(sha):
                    info = tarfile.TarInfo('snapshot/' + path)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
            body = raw.getvalue()
            self.archive_bytes.append(len(body))
        else:
            payload = asyncio.run(self.git.fetch(request.full_url, {'Authorization': 'Bearer host-secret'}))
            if '/git/ref/' not in request.full_url:
                payload['sha'] = urlsplit(request.full_url).path.rsplit('/', 1)[-1]
            body = json.dumps(payload).encode()
        response = io.BytesIO(body)
        response.status, response.headers = 200, {}
        return response

    async def search(self, **kwargs):
        args = SearchQuery(operation='search', pattern=kwargs.pop('pattern', 'absent'), **kwargs)
        before = len(self.requests)
        result = await self.service.query(REPO, 'main', args.model_dump(exclude_none=True))
        self.assertEqual(result['read_diagnostics']['upstream_requests'], len(self.requests) - before)
        return result, len(self.requests) - before

    async def test_wide_search_cold_warm_expired_and_new_process_stay_cheap(self):
        for case in ('cold', 'warm', 'expired', 'new-process'):
            if case == 'expired':
                self.now += 121
            if case == 'new-process':
                self.state._cache.clear(); self.state._bytes = 0
            result, calls = await self.search(max_results=1)
            self.assertTrue(result['complete'], result)
            self.assertEqual(result['scanned_files'], 256)
            self.assertLessEqual(calls, 4, (case, calls))
        # The HTTP fixture serves the archive directly; production's signed
        # codeload redirect adds one bounded download, not one call per file.
        self.assertEqual(sum('/git/blobs/' in p for p in self.requests), 0)
        self.assertEqual(sum('?recursive=1' in p for p in self.requests), 3)

    async def test_result_and_suffix_filters_do_not_walk_every_directory(self):
        result, calls = await self.search(pattern='needle', max_results=1)
        self.assertEqual(len(result['items']), 1)
        self.assertLessEqual(calls, 4)
        self.assertEqual(result['scanned_files'], 1)
        self.assertIsNotNone(result['next_cursor'])
        self.state._cache.clear(); self.state._bytes = 0
        result, calls = await self.search(suffix='.py')
        self.assertEqual((result['scanned_files'], calls), (0, 3))

    async def test_cursor_pins_revision_and_returns_every_match_once(self):
        self.git = GitFixture({'a.txt': b'needle 1\nneedle 2\nneedle 3\n', 'b.txt': b'needle 4\n'})
        original = self.git.head
        result, _ = await self.search(pattern='needle', max_results=1)
        matches = result['items'][:]
        self.git.head = self.git.add_commit({'new.txt': b'needle new\n'}, [original], 'advance')
        pages = 1
        while result['next_cursor']:
            result, _ = await self.search(pattern='needle', max_results=1, cursor=result['next_cursor'])
            self.assertEqual(result['resolved_commit'], original)
            matches.extend(result['items']); pages += 1
            self.assertLessEqual(pages, 4)
        self.assertTrue(result['complete'])
        self.assertEqual([(r['path'], r['line']) for r in matches],
                         [('a.txt', 1), ('a.txt', 2), ('a.txt', 3), ('b.txt', 1)])

    async def test_request_budget_resumes_without_rescanning_earlier_files(self):
        self.service.archive = None
        result, calls = await self.search()
        self.assertEqual(calls, 32)
        self.assertEqual(result['stop_reason'], 'request_budget')
        scanned, pages = result['scanned_files'], 1
        while result['next_cursor']:
            result, calls = await self.search(cursor=result['next_cursor'])
            self.assertLessEqual(calls, 32)
            scanned += result['scanned_files']; pages += 1
            self.assertLess(pages, 12)
        self.assertTrue(result['complete'])
        self.assertEqual(scanned, 256)
        self.assertEqual(sum('/git/blobs/' in p for p in self.requests), 256)

    async def test_forged_changed_query_or_changed_policy_cursor_fails_before_network(self):
        result, _ = await self.search(pattern='needle', max_results=1)
        before = len(self.requests)
        for cursor, pattern in [(result['next_cursor'] + 'x', 'needle'), (result['next_cursor'], 'changed')]:
            with self.assertRaisesRegex(BridgeError, 'candidate_evidence_mismatch'):
                await self.search(pattern=pattern, max_results=1, cursor=cursor)
        self.settings.repositories[REPO]['program_prefixes'] = ['other']
        with self.assertRaisesRegex(BridgeError, 'candidate_evidence_mismatch'):
            await self.search(pattern='needle', max_results=1, cursor=result['next_cursor'])
        self.assertEqual(len(self.requests), before)

    async def test_skipped_files_are_not_lost_across_pages(self):
        self.git = GitFixture({'a.bin': b'\x00', 'b.txt': b'needle\n', 'c.txt': b'needle\n'})
        result, _ = await self.search(pattern='needle', max_results=1)
        result, _ = await self.search(pattern='needle', max_results=1, cursor=result['next_cursor'])
        self.assertIsNone(result['next_cursor'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['total_skipped_count'], 1)

    async def test_large_or_forbidden_subtree_is_not_archived(self):
        self.git = GitFixture({**{f'allowed/{i}.txt': b'needle' for i in range(16)},
                               'private/secret': b'hidden', 'large.bin': b'x' * (17 * 1024 * 1024)})
        self.settings.repositories[REPO] = {'ref': 'main', 'program_prefixes': ['allowed']}
        result, calls = await self.search(pattern='needle')
        self.assertEqual(len(result['items']), 16)
        archived = [p.rsplit('/', 1)[-1] for p in self.requests if '/tarball/' in p]
        self.assertEqual(len(archived), 1)
        self.assertEqual({e['path'] for e in self.git.trees[archived[0]]['tree']}, {f'{i}.txt' for i in range(16)})
        self.assertLessEqual(calls, 4)

    async def test_provider_truncation_never_becomes_a_complete_search(self):
        original = self.git.fetch
        async def truncated(url, headers):
            response = await original(url, headers)
            if '?recursive=1' in url:
                response['truncated'] = True
            return response
        self.git.fetch = truncated
        result, calls = await self.search(suffix='.py')
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'upstream_tree_truncated')
        self.assertTrue(result['narrower_prefix_required'])
        self.assertEqual(calls, 3)

    async def test_archive_corruption_is_not_silently_accepted(self):
        async def corrupt(repo, sha, headers, entries):
            return {p: b'wrong' for p in entries}
        self.service.archive = corrupt
        with self.assertRaisesRegex(BridgeError, 'invalid_upstream_response'):
            await self.search()

    async def test_pacing_pause_returns_cursor_and_retry_delay(self):
        self.service.archive = None
        original = self.git.fetch
        async def busy(url, headers):
            if '/git/blobs/' in url:
                raise BridgeError('github_transport_busy', 503, retry_after=1)
            return await original(url, headers)
        self.git.fetch = busy
        result, _ = await self.search()
        self.assertEqual(result['stop_reason'], 'github_transport_busy')
        self.assertEqual(result['retry_after'], 1)
        self.assertIsNotNone(result['next_cursor'])
        self.git.fetch = original
        result, _ = await self.search(cursor=result['next_cursor'])
        self.assertGreater(result['scanned_files'], 0)


if __name__ == '__main__':
    unittest.main()
