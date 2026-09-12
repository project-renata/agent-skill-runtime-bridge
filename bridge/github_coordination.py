"""Deployment-wide upstream admission, cooldowns and short-lived identity reuse.

The existing TLS Redis stores only hashed scopes and encrypted identity responses.
Redis failure never falls back to uncoordinated upstream traffic. It never grants
authentication: JWT/JTI, token expiry, scopes and owner checks stay in the provider.
"""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import math
import os
import time
import uuid

from .core import BridgeError

ADMIT = '''
local nowparts = redis.call('TIME')
local now = tonumber(nowparts[1]) + tonumber(nowparts[2]) / 1000000
local untiltime = tonumber(redis.call('GET', KEYS[1]) or '0')
if untiltime > now then return {'cooldown', tostring(math.ceil(untiltime-now))} end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return {'busy', '1'} end
local tokens = tonumber(redis.call('HGET', KEYS[3], 'tokens') or '16')
local last = tonumber(redis.call('HGET', KEYS[3], 'time') or tostring(now))
tokens = math.min(16, tokens + math.max(0, now-last)*8)
if tokens < 1 then return {'busy', '1'} end
redis.call('HSET', KEYS[3], 'tokens', tokens-1, 'time', now)
redis.call('EXPIRE', KEYS[3], 60)
redis.call('ZADD', KEYS[2], now+45, ARGV[1])
redis.call('EXPIRE', KEYS[2], 46)
return {'ok', '0'}
'''

COOLDOWN = '''
local now = tonumber(redis.call('TIME')[1])
local deadline = now + tonumber(ARGV[1])
local old = tonumber(redis.call('GET', KEYS[1]) or '0')
if deadline > old then redis.call('SET', KEYS[1], deadline, 'EX', ARGV[1]) end
return 1
'''


def retry_delay(status, headers, error=None, now=None):
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    def number(key):
        value = headers.get(key, '')
        return int(value) if value.isdigit() and len(value) <= 12 else None
    remaining, reset, retry = (number(k) for k in
                                ('x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after'))
    if not (remaining == 0 or error is not None and error.code == 'github_rate_limited'):
        return None
    # A secondary throttle's hourly reset is not its retry deadline.
    delays = [retry] if retry is not None and retry > 0 else []
    if remaining == 0 and reset is not None:
        delays.append(reset - (time.time() if now is None else now))
    return max(1, math.ceil(max(delays))) if any(d > 0 for d in delays) else 60


class GitHubCoordinator:
    def __init__(self, redis, encryption_key):
        from cryptography.fernet import Fernet
        self.redis, self.cipher = redis, Fernet(encryption_key)

    def key(self, credential, part):
        # One Redis hash slot per credential; no tokens, repository paths or URLs.
        return 'runtime-bridge-transport:{' + credential.hex() + '}:' + part

    def command(self, method, *args, **kwargs):
        try:
            return getattr(self.redis, method)(*args, **kwargs)
        except Exception:
            raise BridgeError('github_coordination_unavailable', 503, retry_after=5) from None

    def acquire(self, credential, *, identity=False):
        token = uuid.uuid4().hex
        lease = self.key(credential, 'identity-leases' if identity else 'leases')
        keys = (self.key(credential, 'cooldown'), lease, self.key(credential, 'pace'))
        status, delay = self.command('eval', ADMIT, 3, *keys, token, 1 if identity else 4)
        if status == 'cooldown':
            raise BridgeError('github_rate_limited', 429, retry_after=int(delay))
        if status != 'ok':
            raise BridgeError('github_transport_busy', 503, retry_after=int(delay))
        return lease, token

    def release(self, lease):
        # Leases expire even if a worker disappears. Release failure must not hide
        # a completed write receipt or trigger a replay of an acknowledged effect.
        try:
            self.redis.zrem(*lease)
        except Exception:
            pass

    @contextmanager
    def upstream(self, credential):
        lease = self.acquire(credential)
        try:
            yield
        finally:
            self.release(lease)

    def observe(self, credential, status, headers, error=None):
        delay = retry_delay(status, headers, error)
        if delay is not None:
            self.command('eval', COOLDOWN, 1, self.key(credential, 'cooldown'), delay)

    def identity_key(self, credential, url):
        return self.key(credential, 'identity:' + hashlib.sha256(url.encode()).hexdigest())

    def read_identity(self, credential, url):
        value = self.command('get', self.identity_key(credential, url))
        if value is None:
            return None
        try:
            return json.loads(self.cipher.decrypt(value.encode(), ttl=60))
        except Exception:
            return None

    def cache_identity(self, credential, url, response):
        if response.status_code != 200 or len(response.content) > 65536:
            return
        value = {'body': response.text, 'headers': {k: v for k, v in response.headers.items()
                 if k.lower() == 'x-oauth-scopes'}}
        encrypted = self.cipher.encrypt(json.dumps(value).encode()).decode()
        self.command('set', self.identity_key(credential, url), encrypted, ex=60)


@lru_cache(maxsize=2)
def _from_config(url, key):
    from redis import Redis
    if not url.startswith('rediss://'):
        raise BridgeError('github_coordination_requires_tls', 503)
    try:
        client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2,
                                socket_timeout=2, max_connections=16)
        return GitHubCoordinator(client, key)
    except Exception:
        raise BridgeError('github_coordination_unavailable', 503) from None


def coordinator_from_env(env=None):
    env = os.environ if env is None else env
    url = env.get('BRIDGE_OAUTH_REDIS_URL') or env.get('REDIS_URL')
    if not url:
        return None
    if url.startswith('redis://') and not env.get('BRIDGE_OAUTH_REDIS_URL'):
        url = 'rediss://' + url[len('redis://'):]
    return _from_config(url, env.get('BRIDGE_OAUTH_ENCRYPTION_KEY', ''))
