"""Independent workers against shared Redis, executing the real admission Lua."""
import asyncio
import time
import unittest
from unittest.mock import patch

import fakeredis
import httpx
from cryptography.fernet import Fernet

from bridge.core import BridgeError
from bridge.github_coordination import GitHubCoordinator, retry_delay
from bridge.mcp_server import OAuthGitHubClient, OwnerGitHubProvider
from bridge.transport_cache import credential_key
from test_oauth_availability import identity, upstream_response
from key_value.aio.stores.memory import MemoryStore


class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        key = Fernet.generate_key()
        self.a, self.b = GitHubCoordinator(self.redis, key), GitHubCoordinator(self.redis, key)
        self.credential = credential_key({'Authorization': 'Bearer first'})

    async def test_cooldown_and_concurrency_apply_across_workers_and_release(self):
        leases = [self.a.acquire(self.credential) for _ in range(4)]
        with self.assertRaisesRegex(BridgeError, 'github_transport_busy'):
            self.b.acquire(self.credential)
        self.a.release(leases.pop())
        self.b.release(self.b.acquire(self.credential))
        for lease in leases:
            self.a.release(lease)
        self.a.observe(self.credential, 429, {'retry-after': '40'}, BridgeError('github_rate_limited'))
        with self.assertRaisesRegex(BridgeError, 'github_rate_limited') as caught:
            self.b.acquire(self.credential)
        self.assertTrue(1 <= caught.exception.details['retry_after'] <= 40)
        other = credential_key({'Authorization': 'Bearer second'})
        self.b.release(self.b.acquire(other))
        self.redis.delete(self.a.key(self.credential, 'cooldown'))
        self.b.release(self.b.acquire(self.credential))

    async def test_release_and_lease_expiry_allow_recovery_without_replay(self):
        lease = self.a.acquire(self.credential)
        key, member = lease
        self.redis.zadd(key, {member: time.time() - 1})
        self.b.release(self.b.acquire(self.credential))
        self.assertIsNone(self.redis.zscore(key, member))

    async def test_short_cooldown_cannot_shorten_existing_provider_deadline(self):
        self.a.observe(self.credential, 429, {'retry-after': '120'}, BridgeError('github_rate_limited'))
        original = self.redis.get(self.a.key(self.credential, 'cooldown'))
        self.b.observe(self.credential, 429, {'retry-after': '5'}, BridgeError('github_rate_limited'))
        self.assertEqual(self.redis.get(self.a.key(self.credential, 'cooldown')), original)

    async def test_secondary_limit_does_not_wait_for_hourly_reset(self):
        error = BridgeError('github_rate_limited')
        self.assertEqual(retry_delay(403, {'x-ratelimit-remaining': '1000',
            'x-ratelimit-reset': '5000', 'retry-after': '30'}, error, now=1000), 30)
        self.assertEqual(retry_delay(403, {'x-ratelimit-remaining': '0',
            'x-ratelimit-reset': '1100', 'retry-after': '30'}, error, now=1000), 100)
        self.assertIsNone(retry_delay(403, {}, BridgeError('github_forbidden')))

    async def test_shared_pacing_is_bounded_but_allows_normal_bursts(self):
        for _ in range(16):
            self.a.release(self.a.acquire(self.credential))
        with self.assertRaisesRegex(BridgeError, 'github_transport_busy'):
            self.b.acquire(self.credential)

    async def test_redis_failure_fails_closed_before_upstream(self):
        client = OAuthGitHubClient(self.a)
        with patch.object(self.redis, 'get', side_effect=RuntimeError('offline')), \
                patch.object(httpx.AsyncClient, 'get') as network:
            with self.assertRaisesRegex(BridgeError, 'github_coordination_unavailable'):
                await client.get('https://api.github.com/user', headers={'Authorization': 'Bearer first'})
        network.assert_not_called()

    async def test_identity_success_is_encrypted_shared_and_not_cached_past_60_seconds(self):
        client_a, client_b = OAuthGitHubClient(self.a), OAuthGitHubClient(self.b)
        calls = []
        async def get(client, url, **kwargs):
            calls.append(url)
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            headers = {'Authorization': 'Bearer first'}
            await client_a.get('https://api.github.com/user', headers=headers)
            await client_b.get('https://api.github.com/user', headers=headers)
            self.assertEqual(len(calls), 1)
            key = self.a.identity_key(self.credential, 'https://api.github.com/user')
            self.assertNotIn('123', self.redis.get(key))
            self.assertNotIn('first', key)
            self.assertTrue(0 < self.redis.ttl(key) <= 60)
            with patch('time.time', return_value=time.time() + 61):
                await client_b.get('https://api.github.com/user', headers=headers)
            self.assertEqual(len(calls), 2)

    async def test_limited_identity_is_not_retried_by_another_worker(self):
        clients = [OAuthGitHubClient(self.a), OAuthGitHubClient(self.b)]
        calls = []
        async def get(client, url, **kwargs):
            calls.append(url)
            return upstream_response(url, status=429, headers={'retry-after': '60'})
        with patch.object(httpx.AsyncClient, 'get', get):
            response = await clients[0].get('https://api.github.com/user', headers={'Authorization': 'Bearer first'})
            self.assertEqual(response.status_code, 429)
            with self.assertRaisesRegex(BridgeError, 'github_rate_limited'):
                await clients[1].get('https://api.github.com/user', headers={'Authorization': 'Bearer first'})
        self.assertEqual(len(calls), 1)

    async def test_shared_identity_never_skips_expiry_jti_scopes_or_owner_checks(self):
        provider = OwnerGitHubProvider(allowed_user_ids=['123'], coordinator=self.a,
            client_id='test-client', client_secret='test-secret', base_url='https://bridge.example',
            required_scopes=['read:user'], jwt_signing_key='s' * 40, client_storage=MemoryStore())
        provider.get_routes(mcp_path='/mcp')
        provider, token = await identity(provider=provider, upstream_expires_in=30)
        calls = []
        async def get(client, url, **kwargs):
            calls.append(url)
            return upstream_response(url)
        with patch.object(httpx.AsyncClient, 'get', get):
            self.assertIsNotNone(await provider.verify_token(token))
            self.assertIsNotNone(await provider.verify_token(token))
            self.assertEqual(provider._token_validator._cache._ttl, 0)
            provider.allowed_user_ids = frozenset(['456'])
            self.assertIsNone(await provider.verify_token(token))
            provider.allowed_user_ids = frozenset(['123'])
            with patch('bridge.mcp_server.time.time', return_value=time.time() + 31):
                self.assertIsNone(await provider.verify_token(token))
            await provider._jti_mapping_store.delete(key='test-upstream-jti')
            self.assertIsNone(await provider.verify_token(token))
            self.assertEqual(calls, ['https://api.github.com/user'])


if __name__ == '__main__':
    unittest.main()
