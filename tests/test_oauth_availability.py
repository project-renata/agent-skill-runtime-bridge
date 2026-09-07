"""OAuth quota failures never revoke credentials or bypass authentication."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from key_value.aio.stores.memory import MemoryStore
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet

from bridge.mcp_server import OwnerGitHubProvider, OAuthAvailabilityMiddleware, create_server
from test_bridge import settings


async def identity(upstream='test-upstream', *, expires_in=3600, upstream_expires_in=3600, provider=None):
    if provider is None:
        provider = OwnerGitHubProvider(
            allowed_user_ids=['123'], client_id='test-client', client_secret='test-secret',
            base_url='https://bridge.example', required_scopes=['read:user'],
            jwt_signing_key='s' * 40, client_storage=MemoryStore())
        provider.get_routes(mcp_path='/mcp')
    now = time.time()
    jti = upstream + '-jti'
    await provider._upstream_token_store.put(key=upstream, value=UpstreamTokenSet(
        upstream_token_id=upstream, access_token=upstream, refresh_token=None,
        refresh_token_expires_at=None, expires_at=now + upstream_expires_in, token_type='Bearer',
        scope='read:user', client_id='test-client', created_at=now), ttl=3600)
    await provider._jti_mapping_store.put(key=jti, value=JTIMapping(
        jti=jti, upstream_token_id=upstream, created_at=now), ttl=3600)
    token = provider.jwt_issuer.issue_access_token(
        client_id='test-client', scopes=['read:user'], jti=jti, expires_in=expires_in)
    return provider, token


def upstream_response(url, *, status=200, headers=None):
    return httpx.Response(status, request=httpx.Request('GET', url),
        headers=headers or {'x-oauth-scopes': 'read:user'},
        json={'id': 123} if url.endswith('/user') else [])


class OAuthAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_cache_is_bounded_short_and_does_not_extend_jwt_or_owner_access(self):
        provider, token = await identity()
        calls = []
        async def get(client, url, **kwargs):
            calls.append(url)
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            self.assertIsNotNone(await provider.verify_token(token))
            self.assertIsNotNone(await provider.verify_token(token))
            self.assertEqual(len(calls), 2)
            self.assertEqual(provider._token_validator._cache._ttl, 60)
            self.assertEqual(provider._token_validator._cache._max_size, 128)
            provider.allowed_user_ids = frozenset(['456'])
            self.assertIsNone(await provider.verify_token(token))
            provider.allowed_user_ids = frozenset(['123'])
            _, expired = await identity(expires_in=-3600, provider=provider)
            self.assertIsNone(await provider.verify_token(expired))
            self.assertEqual(len(calls), 2)
            with patch('fastmcp.utilities.token_cache.time.time', return_value=time.time() + 61):
                self.assertIsNotNone(await provider.verify_token(token))
            self.assertEqual(len(calls), 4)
            await provider._jti_mapping_store.delete(key='test-upstream-jti')
            self.assertIsNone(await provider.verify_token(token))
            self.assertEqual(len(calls), 4)

    async def call_mcp(self, provider, tokens, protected_calls=None):
        server = create_server(settings(), provider)
        if protected_calls is not None:
            @server.tool()
            def protected_action():
                protected_calls.append('executed')
                return {'ok': True}
        inner = server.http_app(path='/mcp', stateless_http=True, json_response=True)
        app = OAuthAvailabilityMiddleware(inner)
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://bridge.example') as client:
                return await asyncio.gather(*(client.post('/mcp',
                    headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': index, **(
                        {'method': 'tools/call', 'params': {'name': 'protected_action', 'arguments': {}}}
                        if protected_calls is not None else {'method': 'tools/list'})})
                    for index, token in enumerate(tokens)))

    async def test_rate_limit_is_503_without_auth_challenge_or_tool_execution(self):
        for upstream_status, headers in [
            (403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(int(time.time()) + 90)}),
            (429, {'retry-after': '45'}),
            (429, {}),
        ]:
            provider, token = await identity()
            calls = []
            protected_calls = []
            async def get(client, url, **kwargs):
                calls.append(url)
                return upstream_response(url, status=upstream_status, headers=headers)
            with patch.object(httpx.AsyncClient, 'get', get):
                response, = await self.call_mcp(provider, [token], protected_calls)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertNotIn('www-authenticate', response.headers)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.json(), {'error': 'github_auth_temporarily_unavailable'})
            delay = int(response.headers['retry-after'])
            self.assertTrue(1 <= delay <= 90)
            if upstream_status == 429:
                self.assertEqual(delay, 45 if headers else 60)
            self.assertEqual(len(calls), 1)
            self.assertEqual(protected_calls, [])

    async def test_success_cache_cannot_extend_an_expired_upstream_token(self):
        provider, token = await identity(upstream_expires_in=30)
        calls = []
        async def get(client, url, **kwargs):
            calls.append(url)
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            verified = await provider.verify_token(token)
            self.assertIsNotNone(verified)
            self.assertLessEqual(verified.expires_at, int(time.time()) + 30)
            with patch('bridge.mcp_server.time.time', return_value=time.time() + 31):
                self.assertIsNone(await provider.verify_token(token))
            self.assertEqual(len(calls), 2)

    async def test_upstream_5xx_is_temporary_unavailability_without_execution_or_retry(self):
        for status in (500, 503):
            provider, token = await identity()
            calls, protected_calls = [], []
            async def get(client, url, **kwargs):
                calls.append(url)
                return upstream_response(url, status=status)
            with patch.object(httpx.AsyncClient, 'get', get):
                response, = await self.call_mcp(provider, [token], protected_calls)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers['retry-after'], '60')
            self.assertNotIn('www-authenticate', response.headers)
            self.assertEqual(response.json(), {'error': 'github_auth_temporarily_unavailable'})
            self.assertEqual(len(calls), 1)
            self.assertEqual(protected_calls, [])

    async def test_upstream_transport_failure_is_unavailable_without_execution_or_retry(self):
        for failure in (httpx.ReadTimeout, httpx.ConnectError):
            provider, token = await identity()
            calls, protected_calls = [], []
            async def get(client, url, **kwargs):
                calls.append(url)
                raise failure('synthetic upstream unavailable')
            with patch.object(httpx.AsyncClient, 'get', get):
                response, = await self.call_mcp(provider, [token], protected_calls)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers['retry-after'], '60')
            self.assertNotIn('www-authenticate', response.headers)
            self.assertEqual(response.json(), {'error': 'github_auth_temporarily_unavailable'})
            self.assertEqual(len(calls), 1)
            self.assertEqual(protected_calls, [])

    async def test_expired_cached_upstream_transparently_refreshes(self):
        provider, token = await identity(upstream_expires_in=30)
        original = await provider._upstream_token_store.get(key='test-upstream')
        original = original.model_copy(update={'refresh_token': 'test-refresh'})
        await provider._upstream_token_store.put(key='test-upstream', value=original, ttl=3600)
        async def get(client, url, **kwargs):
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            self.assertIsNotNone(await provider.verify_token(token))
            future = time.time() + 31
            renewed = original.model_copy(update={'access_token': 'fresh-upstream', 'expires_at': future + 3600})
            refresh = AsyncMock(return_value=renewed)
            with patch('bridge.mcp_server.time.time', return_value=future), \
                    patch.object(provider, '_try_transparent_refresh', refresh):
                verified = await provider.verify_token(token)
            self.assertIsNotNone(verified)
            self.assertEqual(verified.token, 'fresh-upstream')
            self.assertEqual(verified.expires_at, int(renewed.expires_at))
            refresh.assert_awaited_once()

    async def test_failed_refresh_cannot_reuse_expired_cached_identity(self):
        provider, token = await identity(upstream_expires_in=30)
        original = await provider._upstream_token_store.get(key='test-upstream')
        original = original.model_copy(update={'refresh_token': 'test-refresh'})
        await provider._upstream_token_store.put(key='test-upstream', value=original, ttl=3600)
        async def get(client, url, **kwargs):
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            self.assertIsNotNone(await provider.verify_token(token))
            refresh = AsyncMock(side_effect=RuntimeError('synthetic revoked refresh'))
            with patch('bridge.mcp_server.time.time', return_value=time.time() + 31), \
                    patch.object(provider, '_try_transparent_refresh', refresh):
                self.assertIsNone(await provider.verify_token(token))
            refresh.assert_awaited_once()

    async def test_parallel_rate_limit_and_real_forbidden_are_isolated(self):
        provider, limited = await identity('limited')
        _, forbidden = await identity('forbidden', provider=provider)
        async def get(client, url, **kwargs):
            limited_call = kwargs['headers']['Authorization'] == 'Bearer limited'
            await asyncio.sleep(0)
            return upstream_response(url, status=403,
                headers={'retry-after': '30'} if limited_call else {'x-ratelimit-remaining': '100'})
        with patch.object(httpx.AsyncClient, 'get', get):
            unavailable, denied = await self.call_mcp(provider, [limited, forbidden])
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.headers['retry-after'], '30')
        self.assertEqual(denied.status_code, 401)
        self.assertIn('www-authenticate', denied.headers)
        self.assertNotIn('retry-after', denied.headers)

    async def test_valid_owner_still_works_after_an_independent_throttled_request(self):
        provider, limited = await identity('limited')
        _, valid = await identity('valid', provider=provider)
        async def get(client, url, **kwargs):
            if kwargs['headers']['Authorization'] == 'Bearer limited':
                return upstream_response(url, status=429, headers={'retry-after': '30'})
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            unavailable, success = await self.call_mcp(provider, [limited, valid])
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(success.status_code, 200, success.text)
        self.assertIn('tools', success.json()['result'])


if __name__ == '__main__':
    unittest.main()
