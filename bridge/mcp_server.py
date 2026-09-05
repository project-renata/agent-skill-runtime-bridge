"""Optional CPython MCP adapter. Canonical programs and the core stay SDK-free."""
import asyncio
import json
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from bridge.core import Settings, handle
from bridge.execution import execute_subprocess
from bridge.http import fetch_json, send_json


class WriteIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_commit: str = Field(pattern=r'^[0-9a-f]{40}$', description='Source commit returned by the preceding read call.')
    message: str = Field(min_length=1, max_length=500)


class OwnerGitHubProvider(GitHubProvider):
    """A valid GitHub login alone never grants access to the deployment's repo."""
    def __init__(self, *, allowed_user_ids, **kwargs):
        self.allowed_user_ids = frozenset(allowed_user_ids)
        if not self.allowed_user_ids:
            raise ValueError('An explicit GitHub user allowlist is required')
        super().__init__(**kwargs)

    async def verify_token(self, token):
        verified = await super().verify_token(token)
        if verified and str(verified.claims.get('sub', '')) in self.allowed_user_ids:
            return verified
        return None


def create_server(settings, auth, *, fetch=fetch_json, send=send_json, execute=execute_subprocess):
    if auth is None:
        raise ValueError('MCP authentication is required')
    mcp = FastMCP('Agent Skill Runtime Bridge', version='0.3.0', auth=auth,
        mask_error_details=True, strict_input_validation=True,
        instructions='Call list_runtime_targets to inspect allowed repositories, refs and paths. '
        'Use run_readonly_skill to execute trusted canonical Python against an immutable snapshot. '
        'For writes, first read all existing target files and retain source.commit. '
        'Pass that commit to run_write_skill. On branch_conflict, read again and reconcile before retrying.')

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False})
    def list_runtime_targets() -> dict:
        """Use this to discover the deployment's allowed repositories, branches, Python program paths and data/write paths."""
        return {'repositories': settings.repositories}

    async def run(arguments):
        # Isolate blocking GitHub I/O and the child process from the ASGI loop.
        def invoke():
            return asyncio.run(handle(json.dumps(arguments, ensure_ascii=False).encode(),
                'Bearer ' + settings.key, settings, fetch, execute, send))
        status, result = await asyncio.to_thread(invoke)
        if status != 200:
            raise ToolError(result['error']['code'])
        return result

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': True})
    async def run_readonly_skill(repository: str, ref: str, program: str, files: list[str], input: dict) -> dict:
        """Use this to run an allowed repository Python program with explicitly loaded files. Temporary file edits are discarded. Returns JSON result and source.commit for a later write."""
        return await run(dict(repository=repository, ref=ref, program=program, files=files, input=input))

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': True, 'idempotentHint': False})
    async def run_write_skill(repository: str, ref: str, program: str, files: list[str], input: dict, write: WriteIntent) -> dict:
        """Use this for an authorized batch of repository edits, including deletion. Load every existing target in files and supply the preceding source.commit as write.expected_commit. All accepted changes share one commit. A conflict requires a fresh read and reconciliation."""
        return await run(dict(repository=repository, ref=ref, program=program, files=files, input=input, write=write.model_dump()))

    return mcp


def production_app(env=None):
    env = os.environ if env is None else env
    required = ('BRIDGE_MCP_BASE_URL', 'BRIDGE_OAUTH_CLIENT_ID', 'BRIDGE_OAUTH_CLIENT_SECRET',
                'BRIDGE_OAUTH_ALLOWED_USER_IDS', 'BRIDGE_OAUTH_REDIS_URL',
                'BRIDGE_OAUTH_SIGNING_KEY', 'BRIDGE_OAUTH_ENCRYPTION_KEY')
    if not all(env.get(key) for key in required):
        async def unavailable(request):
            return JSONResponse({'error': 'mcp_oauth_not_configured'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        return Starlette(routes=[Route('/{path:path}', unavailable, methods=['GET', 'POST', 'DELETE'])])
    base = env['BRIDGE_MCP_BASE_URL'].rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('BRIDGE_MCP_BASE_URL must be an HTTPS origin')
    redis_url = env['BRIDGE_OAUTH_REDIS_URL']
    if not redis_url.startswith('rediss://'):
        raise ValueError('OAuth Redis requires TLS')
    allowed = json.loads(env['BRIDGE_OAUTH_ALLOWED_USER_IDS'])
    if not isinstance(allowed, list) or not allowed or any(not isinstance(x, str) or not x.isdigit() for x in allowed):
        raise ValueError('Expected numeric GitHub user IDs as a JSON string array')
    if len(env['BRIDGE_OAUTH_SIGNING_KEY']) < 32:
        raise ValueError('OAuth signing key must contain at least 32 characters')
    storage = FernetEncryptionWrapper(
        key_value=RedisStore(url=redis_url, default_collection='runtime-bridge-oauth'),
        fernet=Fernet(env['BRIDGE_OAUTH_ENCRYPTION_KEY']))
    auth = OwnerGitHubProvider(allowed_user_ids=allowed,
        client_id=env['BRIDGE_OAUTH_CLIENT_ID'], client_secret=env['BRIDGE_OAUTH_CLIENT_SECRET'],
        base_url=base, required_scopes=['read:user'], client_storage=storage,
        jwt_signing_key=env['BRIDGE_OAUTH_SIGNING_KEY'],
        fastmcp_access_token_expiry_seconds=3600,
        allowed_client_redirect_uris=['https://chatgpt.com/connector/oauth/*',
                                      'https://chatgpt.com/connector_platform_oauth_redirect'])
    server = create_server(Settings.from_env(env), auth)
    app = server.http_app(path='/mcp', stateless_http=True, json_response=True,
                          host_origin_protection=True, allowed_hosts=[parsed.netloc],
                          allowed_origins=['https://chatgpt.com', base])
    app.add_middleware(NoStoreMiddleware)
    return app


class NoStoreMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def no_store(message):
            if message['type'] == 'http.response.start':
                message['headers'] = [(k,v) for k,v in message.get('headers', []) if k.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]
            await send(message)
        await self.app(scope, receive, no_store)
