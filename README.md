# Agent Skill Runtime Bridge

Run an operator-trusted Python program directly from its GitHub canonical version.
The same `run(repository_root, input) -> JSON-compatible value` function works locally,
on Vercel CPython, and in the Cloudflare Python adapter. Programs contain no vendor metadata.

## Status and scope

Version 0.1 is the **read-only first milestone**, not the complete read/write MVP.
It retrieves one Python module plus explicitly requested files, fixes every file to
one resolved commit, invokes `run`, and returns the result and source hashes.
Updating a program on the configured branch requires no Bridge deployment.

The portable subset is standard-library Python without subprocesses, native packages,
local sibling imports, or direct network dependencies. Dependency files can be passed
in `files` and read with normal file I/O. Automatic import/dependency resolution and
atomic GitHub writes are not implemented yet. Heavy computation belongs elsewhere.

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
  "result": {"files": [{"path": "notes/today.md", "title": "Today"}], "count": 1},
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

## Deployment configuration

Configure these on the hosting provider, never commit them:

- `BRIDGE_API_KEY`: random ASCII secret, at least 32 characters.
- `BRIDGE_GITHUB_TOKEN`: fine-grained token restricted to the required repositories,
  with **Contents: read-only** for this milestone. Omit for public-only repositories.
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
