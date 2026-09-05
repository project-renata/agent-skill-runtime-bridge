# Agent Skill Runtime Bridge

Run an operator-trusted Python program directly from its GitHub canonical version.
The same `run(repository_root, input) -> JSON-compatible value` function works locally,
on Vercel CPython, and in the Cloudflare Python adapter. Programs contain no vendor metadata.

## Status and scope

Version 0.2 supports reads and opt-in atomic UTF-8 file commits. It retrieves one
Python module plus explicitly requested files, fixes every file to one resolved
commit, invokes `run`, and returns the result and source hashes. Programs write
ordinary temporary files; the Bridge validates and persists an authorized batch.
Updating a program on the configured branch requires no Bridge deployment.

The portable subset is standard-library Python without subprocesses, native packages,
local sibling imports, or direct network dependencies. Dependency files can be passed
in `files` and read with normal file I/O. Automatic import/dependency resolution
is not implemented yet. Heavy computation belongs elsewhere.

## Run a normal program locally

```sh
echo '{"files":["README.md"]}' | python3 examples/markdown_titles/main.py .
python3 -m unittest discover -s tests -v
```

The example exposes `run(root, request)` and a standalone command-line entrypoint.
The Bridge invokes its function; it does not rewrite the program.

## HTTP contract

`POST /api/run` on Vercel, `POST /` on Cloudflare. JSON body:

```json
{
  "repository": "owner/private-skills",
  "ref": "main",
  "program": "skills/markdown-titles/program/main.py",
  "files": ["notes/today.md"],
  "input": {"files": ["notes/today.md"]}
}
```

Use `Content-Type: application/json` and `Authorization: Bearer <BRIDGE_API_KEY>`.
The `files` array describes snapshot inputs, while `input` is passed unchanged to
the program. Paths are relative to the source repository root. Only configured
repositories, branch names (or configured full commit hashes), program prefixes,
and data prefixes are accepted. Prefixes match complete path components.

Successful response:

```json
{
  "ok": true,
    "result": {"files": [{"path": "notes/today.md", "title": "Today", "line_count": 3}], "count": 1},
  "source": {
    "repository": "owner/private-skills",
    "ref": "main",
    "commit": "resolved-40-character-commit-sha",
    "program": "skills/markdown-titles/program/main.py",
    "sha256": "sha256-of-executed-program"
  }
}
```

Errors use `{"ok":false,"error":{"code":"unauthorized"}}` with a non-2xx status.
Responses are `Cache-Control: no-store`; stack traces and program logs are omitted.
GET returns service metadata, not configuration or proof of private access.

### Saving changes

Without a `write` field, temporary modifications are discarded. To commit the
program's changes, add:

```json
"write": {
  "message": "Update notes",
  "expected_commit": "the-40-character-commit-from-the-previous-read"
}
```

The branch is the request's `ref`. The operator must also configure `write_refs`
and `write_prefixes` for this repository. `additional_refs` optionally permits
other read/execution branches. For example, add these policy fields:

```json
"additional_refs": ["bridge-validation"],
"write_refs": ["bridge-validation"],
"write_prefixes": ["notes"]
```

Load existing files to be changed in `files`; new paths need not be loaded.
Only UTF-8 regular files under `write_prefixes` can be committed. The Bridge
detects create/update/delete operations, preserves executable modes on existing
files, validates the whole batch, creates one tree/commit, and performs a
fast-forward-only branch update. Files outside the requested snapshot are
preserved via the base tree. Existing but unread files are never overwritten.

The response adds `write: {commit, changed}`. A no-op returns the source commit
with an empty `changed` list. Stale `expected_commit` or a concurrently changed
branch returns 409. Read the new state and reconsider the edit before retrying;
do not blindly replay an operation. A network failure after updating the branch
can leave an ambiguous result: inspect the branch before retrying. A failed ref
update may leave unreachable Git objects but does not partially update files.

The ordinary `examples/edit_note/main.py` program accepts
`input: {"changes": {"notes/today.md": "new text", "notes/old.md": null}}`.
The caller controls write intent; program input alone never grants permission.

## Deployment configuration

Configure these on the hosting provider, never commit them:

- `BRIDGE_API_KEY`: random ASCII secret, at least 32 characters.
- `BRIDGE_GITHUB_TOKEN`: fine-grained token restricted to the required repositories.
  **Contents: read-only** suffices for the implemented probe. For a long-term memory
  integration that saves changes, provision **Contents: read and write**
  on the single intended repository, and enable the appropriate write policy.
  Omit the token for public read-only repositories.
  Select the lifetime to match the intended service; this project does not require
  a 30-day expiration. Organization lifetime policies still apply.
- `BRIDGE_REPOSITORIES`: JSON policy, for example:

```json
{
  "owner/private-skills": {
    "ref": "main",
    "program_prefixes": ["skills"],
    "data_prefixes": ["notes"]
  }
}
```

Vercel: `vercel link`, add the three environment variables for the intended deployment
environment, then `vercel --prod`. The Python function uses only the standard library.

Cloudflare: `uv sync`, `uv run pywrangler dev` or `uv run pywrangler deploy`.
The tested toolchain is Node 22, uv 0.12.10, workers-py 1.17.1, Wrangler 4.129.0.
When system Node/uv differ, use an isolated tool invocation:

```sh
npm exec --yes --package=node@22 --package=wrangler@4.129.0 -- uv tool run --from 'uv==0.12.10' uv run pywrangler dev
```

Wrangler's build command stages only `worker.py` and the three portable core modules
into `.cloudflare-build`. Generated packages and local environments stay outside it.
Set secrets with `uv run pywrangler secret put VARIABLE_NAME`. Use `.dev.vars` for
local tests only; it is ignored by Git and Vercel. The Worker uses the same core
with Cloudflare's HTTP transport and an inline execution adapter.

Use a host-side client that can keep the API key secret. An HTTP endpoint by itself
does not install an Action or MCP connector into an agent; configure and verify that
integration separately. Do not put the bearer key in URLs or conversation prompts.

## Execution boundary

This is **not an untrusted-code sandbox**. Only let trusted maintainers write to
allowed program prefixes and the configured branch. A caller holding the API key
can run any program in those allowed prefixes and retrieve allowed data.

Vercel runs programs in a child process with an empty credential environment,
10-second timeout, bounded result pipe, and per-request temporary snapshot. This
is fault containment, not a complete filesystem/network sandbox.
Cloudflare invokes trusted Python synchronously in its Worker isolate; platform
CPU limits apply. Cloudflare does not have equivalent child-process isolation.
Never claim arbitrary hostile programs are safe on either adapter.

The protocol rejects absolute/parent paths, symlinks, submodules and oversized
files. Git tree modes and blob hashes are verified. GitHub redirects are disabled.
Limits: 64 KiB request, 32 snapshot files, 512 KiB per file, 2 MiB snapshot,
512 KiB result. These bounds apply to the first milestone; large jobs are excluded.

## Validation

The conformance suite covers the actual example function through both execution
adapters on CPython, auth-before-network, policy, Unicode paths, hash validation,
symlink rejection, fresh snapshots, secret environment separation, timeout,
output limits, and fetching changed source without rebuilding the Bridge.
Mock transport tests are not evidence of a hosted Cloudflare Worker or a Web agent.
Record hosted and agent integration results separately.

Runtime references:
- https://vercel.com/docs/functions/runtimes/python/api-directory
- https://developers.cloudflare.com/workers/languages/python/
- https://developers.cloudflare.com/workers/languages/python/stdlib/


## ChatGPT Projects: authenticated MCP adapter

The CPython adapter in `bridge/mcp_server.py` exposes `/mcp` with Streamable HTTP:
`list_runtime_targets`, `run_readonly_skill`, and `run_write_skill`. The core and
canonical programs remain independent of FastMCP. The existing `/api/run` endpoint
continues to use its deployment API key. MCP uses a separate OAuth login.

ChatGPT developer-mode connections support OAuth; they cannot send a custom API
key. See [OpenAI authentication](https://developers.openai.com/plugins/build/auth)
and [developer mode](https://developers.openai.com/api/docs/guides/developer-mode).

This adapter uses FastMCP's GitHub OAuth provider, PKCE, encrypted persistent
Redis storage, and a numeric GitHub-user allowlist. OAuth requests `read:user`
only; repository permissions still come from the separately configured,
repository-scoped `BRIDGE_GITHUB_TOKEN`. A different GitHub user's valid login
is rejected. Read and write tools have separate schemas and annotations.

Configure these production variables once, in addition to the original Bridge
variables:

| Variable | Value |
| --- | --- |
| `BRIDGE_MCP_BASE_URL` | Stable HTTPS origin, without a path |
| `BRIDGE_OAUTH_CLIENT_ID` | GitHub OAuth App client ID |
| `BRIDGE_OAUTH_CLIENT_SECRET` | GitHub OAuth App client secret |
| `BRIDGE_OAUTH_ALLOWED_USER_IDS` | JSON array of numeric user IDs as strings |
| `BRIDGE_OAUTH_REDIS_URL` | Persistent Redis connection URL using `rediss://`; optional when Vercel's native Upstash integration provides `REDIS_URL` |
| `BRIDGE_OAUTH_SIGNING_KEY` | Stable random secret, at least 32 characters |
| `BRIDGE_OAUTH_ENCRYPTION_KEY` | Stable Fernet key |

Create the GitHub OAuth App with callback
`https://YOUR-BRIDGE-HOST/auth/callback`. Use persistent shared Redis; in-memory
or function-local file storage will lose registrations across deployments.
The native `REDIS_URL` fallback always connects with TLS, including when its URL
uses the generic `redis://` spelling. An explicit `BRIDGE_OAUTH_REDIS_URL` takes
precedence and must use `rediss://`.
Preserve both encryption and signing keys across deployments. OAuth access-token
refresh is delegated to the provider/client; expiry or revocation of upstream
credentials can still require reauthorization. This is not a guarantee of a
permanent login.

Then redeploy and create a ChatGPT developer-mode app using
`https://YOUR-BRIDGE-HOST/mcp` and OAuth. Complete GitHub sign-in, select the app
in the target project conversation, and test a read followed by an explicitly
authorized write on the validation branch. Verify the returned commit in GitHub
before enabling production memory-writing paths. ChatGPT may ask for write
confirmation in new conversations; that is separate from credential renewal.

Until all OAuth variables are configured, the MCP/OAuth routes return 503
`mcp_oauth_not_configured`; no anonymous execution fallback is provided.
The MCP adapter currently targets Vercel/CPython. Cloudflare continues to serve
the provider-neutral HTTP core; its MCP/OAuth adapter is not implemented.
