"""Optional CPython MCP adapter. Canonical programs and the core stay SDK-free."""
import asyncio
import json
import os
from urllib.parse import urlsplit
from typing import Annotated

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

from bridge.core import (Settings, handle, MAX_FILES, MAX_FILE, MAX_TOTAL,
                         MAX_SNAPSHOT_FILES, MAX_SNAPSHOT_TOTAL, MAX_SNAPSHOT_DIRS,
                         MAX_SNAPSHOT_FILE, MAX_TREE_ENTRIES, MAX_TREE_RESPONSE, MAX_ARCHIVE_BYTES)
from bridge.execution import execute_subprocess
from bridge.http import fetch_json, send_json, fetch_archive


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


def create_server(settings, auth, *, fetch=fetch_json, send=send_json, execute=execute_subprocess, archive=None):
    if auth is None:
        raise ValueError('MCP authentication is required')
    mcp = FastMCP('Agent Skill Runtime Bridge', version='0.4.0', auth=auth,
        mask_error_details=True, strict_input_validation=True,
        instructions='Call list_runtime_targets to inspect allowed repositories, refs and paths. '
        'Use run_readonly_skill to execute trusted canonical Python against an immutable snapshot. '
        'For writes, first read all existing target files and retain source.commit. '
        'Pass that commit to run_write_skill. On branch_conflict, read again and reconcile before retrying. '
        'When a repository advertises authoring, you can create, edit and run Python in that workspace. '
        'Use authoring.ref and authoring.program as the file helper: input={read:[paths]} returns loaded UTF-8 files; '
        'input={changes:{path:content_or_null}} saves a batch through run_write_skill. '
        'First call the helper read-only with files=[] and input={} to obtain the current source.commit; '
        'load existing targets in files before editing. New programs go under authoring.program_prefix, '
        'data under authoring.data_prefix. Write ordinary Python defining run(root,input) that returns JSON. '
        'Then execute the saved program using run_readonly_skill, or run_write_skill to persist its output files. '
        'Read back your source with the helper when revising it. Only operator-trusted Python is supported; '
        'execution is not an untrusted-code sandbox.')

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False})
    def list_runtime_targets() -> dict:
        """Use this to discover the deployment's allowed repositories, branches, Python program paths and data/write paths."""
        result = {'runtime_version': '0.4.0', 'repositories': settings.repositories}
        result['snapshot_usage'] = {
            'files': 'Keep files=["path/file.md"] for explicit files. A trailing slash selects a recursive subtree: files=["memory/story/","memory/fable/"]. Python sees the repository-relative files under root and can use pathlib/rglob without a caller-generated file list.',
            'history': 'On read_all repositories, readonly ref also accepts a full lowercase 40-character commit SHA fetched from that repository. Named refs retain their allowlist. source.commit is the resolved data commit; immutable commits are never write targets.',
            'program_ref': 'Optional readonly program_ref selects the canonical program version in the same repository when it is newer than the data snapshot. Only the program file is overlaid at its canonical path; other files come from ref. Omit for a single-version snapshot. source.program_commit records the resolved code commit. Writes reject program_ref.',
            'safety': 'Directory loads skip symlinks/submodules, reject unsafe paths and fail on truncated trees or limits. Explicit forbidden entries remain errors. No git metadata or Bridge input file is placed in root. Write commit, SHA preconditions and atomic semantics are unchanged.',
            'limits': {'selectors': MAX_FILES - 1, 'file_bytes': MAX_FILE,
                       'explicit_files': MAX_FILES, 'explicit_bytes': MAX_TOTAL,
                       'directory_files': MAX_SNAPSHOT_FILES, 'directory_bytes': MAX_SNAPSHOT_TOTAL,
                       'directory_file_bytes': MAX_SNAPSHOT_FILE,
                       'directories': MAX_SNAPSHOT_DIRS, 'tree_entries': MAX_TREE_ENTRIES,
                       'tree_response_bytes': MAX_TREE_RESPONSE, 'archive_bytes': MAX_ARCHIVE_BYTES,
                       'write_changes': MAX_FILES},
        }
        if any(policy.get('repo_files') for policy in settings.repositories.values()):
            result['repo_files_usage'] = {
                'helper': 'Use repository repo_files.ref and repo_files.program for the canonical file atomics.',
                'read': 'run_readonly_skill: files=[paths], input={read:[paths]}. Text and stat are result.files[path].',
                'write': 'run_write_skill: input={changes:{path:text_or_null},expect:{path:sha256_or_null},read:[receipt_paths]}; write={expected_commit:source.commit,message:description}. Load existing targets in files; do not list absent paths in files.',
                'preconditions': 'expect checks optional per-file SHA-256 (null means absent). A failed preflight returns result.ok=false and result.error.code, with zero changes; inspect both outer ok and result.ok.',
                'receipt': 'result.changes[path] contains operation, before and after. Persisted paths and commit are write.changed and write.commit.',
                'boundaries': 'read_all=true allows all normal repository-relative files. write_all_refs grants repository-wide writes on the listed write_refs; other branches use write_prefixes_by_ref or legacy write_prefixes. Traversal, absolute paths, symlinks and submodules remain rejected. Execution still requires program_prefixes. Load existing files and supply expected_commit for writes; follow the repository OS and Skill validation workflows when editing their assets.',
            }
        if any(policy.get('authoring') for policy in settings.repositories.values()):
            # Some clients omit MCP initialize.instructions from model context.
            # Keep the canonical helper contract in the discovery tool result too.
            result['authoring_usage'] = {
                'helper': 'Use the repository authoring.ref and authoring.program.',
                'current_commit': {'tool': 'run_readonly_skill', 'files': [], 'input': {}},
                'read_source': {'tool': 'run_readonly_skill', 'files': ['REPOSITORY_RELATIVE_PATH'],
                                'input': {'read': ['REPOSITORY_RELATIVE_PATH']}},
                'read_result': 'Actual source text is result.files[path]. Loading a path in files alone does not return its contents.',
                'save': 'Call run_write_skill on the helper with input={changes:{path:source_text}} and write={expected_commit:preceding_source_commit,message:description}. Include every existing target in files.',
                'execute': 'Run the saved .py path under authoring.program_prefix with run_readonly_skill, files listing required data, and JSON input. Python must define run(root,input) returning JSON.',
                'limits': 'Operator-trusted code only; no hostile-code sandbox or dynamic package installation.',
            }
        return result

    async def run(arguments):
        # Isolate blocking GitHub I/O and the child process from the ASGI loop.
        def invoke():
            return asyncio.run(handle(json.dumps(arguments, ensure_ascii=False).encode(),
                'Bearer ' + settings.key, settings, fetch, execute, send, archive))
        status, result = await asyncio.to_thread(invoke)
        if status != 200:
            raise ToolError(result['error']['code'])
        return result

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': True})
    async def run_readonly_skill(repository: str, ref: str, program: str, files: list[str], input: dict,
                                 program_ref: Annotated[str | None, Field(description=
                                     'Optional code ref in the same repository, for example main with a historical data ref. '
                                     'Omit to load code from ref. Only the canonical program file is overlaid; '
                                     'source.program_commit records its resolved commit. Readonly only.')] = None) -> dict:
        """Run canonical Python with repository-relative files or recursive directories (trailing /) in root. read_all repositories accept historical commit SHA refs. Optional program_ref selects the code version independently of the data snapshot. Temporary edits are discarded; source records resolved commits."""
        arguments = dict(repository=repository, ref=ref, program=program, files=files, input=input)
        if program_ref is not None:
            arguments['program_ref'] = program_ref
        return await run(arguments)

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': True, 'idempotentHint': False})
    async def run_write_skill(repository: str, ref: str, program: str, files: list[str], input: dict, write: WriteIntent) -> dict:
        """Use this for an authorized batch of repository edits, including deletion. Load every existing target in files and supply the preceding source.commit as write.expected_commit. All accepted changes share one commit. A conflict requires a fresh read and reconciliation."""
        return await run(dict(repository=repository, ref=ref, program=program, files=files, input=input, write=write.model_dump()))

    return mcp


def production_app(env=None):
    env = os.environ if env is None else env
    redis_url = env.get('BRIDGE_OAUTH_REDIS_URL') or env.get('REDIS_URL', '')
    # Vercel's native Upstash integration injects REDIS_URL. Always use TLS,
    # including when the provider uses the generic redis:// URL spelling.
    if not env.get('BRIDGE_OAUTH_REDIS_URL') and redis_url.startswith('redis://'):
        redis_url = 'rediss://' + redis_url[len('redis://'):]
    required = ('BRIDGE_MCP_BASE_URL', 'BRIDGE_OAUTH_CLIENT_ID', 'BRIDGE_OAUTH_CLIENT_SECRET',
                'BRIDGE_OAUTH_ALLOWED_USER_IDS',
                'BRIDGE_OAUTH_SIGNING_KEY', 'BRIDGE_OAUTH_ENCRYPTION_KEY')
    if not redis_url or not all(env.get(key) for key in required):
        async def unavailable(request):
            return JSONResponse({'error': 'mcp_oauth_not_configured'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        return Starlette(routes=[Route('/{path:path}', unavailable, methods=['GET', 'POST', 'DELETE'])])
    base = env['BRIDGE_MCP_BASE_URL'].rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('BRIDGE_MCP_BASE_URL must be an HTTPS origin')
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
    server = create_server(Settings.from_env(env), auth, archive=fetch_archive)
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
