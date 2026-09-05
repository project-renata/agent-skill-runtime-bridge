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
and data access policies are accepted. Prefixes match complete path components.
`read_all: true` allows any normal file inside the selected repository snapshot;
it does not widen program execution or write permissions.

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
and a write grant (`write_prefixes`, `write_prefixes_by_ref` or `write_all_refs`)
for this repository. `additional_refs` optionally permits
other read/execution branches. For example, add these policy fields:

```json
"additional_refs": ["bridge-validation"],
"write_refs": ["bridge-validation"],
"write_prefixes": ["notes"]
```

Load existing files to be changed in `files`; new paths need not be loaded.
Only UTF-8 regular files allowed by the selected ref's write grant can be committed. The Bridge
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

### Repository directories and immutable history

The existing `files` list accepts both files and directory selectors. A single
trailing `/` selects every normal file recursively beneath that repository path:

```json
{
  "repository": "owner/private-skills",
  "ref": "main",
  "program": "memory/skill/tools/example/scripts/example.py",
  "files": ["memory/story/", "memory/fable/", "AGENTS.md"],
  "input": {}
}
```

The same Python `run(root, input)` can use `Path(root).rglob("*.md")` and ordinary
filesystem reads on Local and Web. Directory loads retain repository-relative
paths; overlapping selectors are deduplicated. Symlinks and submodules are
skipped inside recursive loads and still rejected when explicitly requested.
Git metadata and the Bridge input payload are never materialized into root.
The caller chooses subtrees, without enumerating their files or using a tree API.

For repositories with `read_all: true`, readonly `ref` also accepts a full
lowercase 40-character Git commit SHA. The Bridge resolves it through that
repository's Git commits endpoint and verifies the returned SHA. Unknown commits
fail; named refs retain the current policy allowlist. `source.commit` is always
the resolved data commit. A commit SHA is never a write target, even if listed
in the write policy.

Optional readonly `program_ref` chooses the canonical program version in the
same repository independently of the data snapshot. Use this when the program
was introduced after the historical data commit. It follows the same ref
validation and execution prefix policy. Only that program file is overlaid at
its canonical path; all other selected files come from `ref`.
`source.program_commit` identifies the resolved code revision. Omitting the field
keeps the original single-version behavior. Write requests reject `program_ref`.
MCP `run_readonly_skill.inputSchema.properties.program_ref` explicitly publishes
this optional parameter; runtime version 0.4.0 identifies this schema/capacity
release. After deployment, refresh the app in ChatGPT app settings to pull the
server's current tool definitions. An already imported tool schema can remain
older than the backend; deployment alone does not prove the client refreshed.
Check that the callable schema includes `program_ref` before calling:

```json
{
  "repository": "project-renata/project-renata",
  "ref": "e714840affe01f5019e786f5aaf6d69278380d6c",
  "program_ref": "main",
  "program": "memory/skill/workflows/renata-recall/scripts/recall.py",
  "files": ["memory/story/", "memory/fable/"],
  "input": {"action": "catalog", "paths": ["memory/story/STORIES.md", "memory/fable/FABLES.md"]}
}
```

ChatGPT's [developer-mode documentation](https://developers.openai.com/api/docs/guides/developer-mode#how-to-use)
describes app refresh for updated tools, descriptions and server instructions.

Explicit-file callers retain the 31-selector, 32-file, 512 KiB-per-file and 2 MiB
snapshot limits. Directory-selected files permit 4 MiB each, with a snapshot cap
of 32,768 files, 384 MiB total and 16,384 directories (31 selectors still apply).
The program and explicit file selectors retain their 512 KiB cap, including in
mixed requests. Recursive tree responses are bounded at 32 MiB and 65,536 entries.
These bounded defaults allow more than twice the measured September 2026
Story/Fable inventory: 15,455 files, 171.3 MiB, 6,292 directories, 1.68 MiB largest
file. Unchanged large files are allowed during change collection without copying
the entire baseline into a second dictionary. New/changed files retain the
512 KiB-per-file, 2 MiB-total write limits. Oversized or
truncated snapshots fail before execution with `too_many_snapshot_files`,
`snapshot_too_large`, `too_many_snapshot_directories`, `too_many_snapshot_entries`,
`file_too_large`, `upstream_response_too_large` or `repository_tree_truncated`.
`source.snapshot` reports loaded file/byte counts and skipped entries.

The loader validates the entire manifest before downloading blobs. CPython can
fetch a bounded archive for large selections in small repositories, verify every
selected Git blob hash, and materialize only the requested files. Other loads use
bounded concurrent blob fetches. Archives are streamed with both compressed and
expanded size caps (512 MiB each, including tar overhead); extraction never writes filesystem paths. GitHub credentials
are not forwarded to download redirects. Vercel requests allow up to 300 seconds
for materialization; the Python execution timeout remains 10 seconds.

Directory selectors also work on allowed write branches. The expanded files form
the baseline for the existing create/update/delete diff. `expected_commit`,
unread-file protection, per-ref write scope and the 32-change atomic commit limit
are unchanged. An immutable read is not permission to write its commit: read the
current write branch again before preparing a write transaction.

### Whole-repository access and separate write boundaries per branch

Use explicit whole-repository modes instead of root-like or wildcard path prefixes:

```json
{
  "owner/private-skills": {
    "ref": "main",
    "read_all": true,
    "program_prefixes": ["bridge-bootstrap", "runtime-workspace/programs"],
    "additional_refs": ["runtime-bridge/web-workspace"],
    "write_refs": ["main", "runtime-bridge/web-workspace"],
    "write_all_refs": ["main"],
    "write_prefixes_by_ref": {
      "runtime-bridge/web-workspace": ["runtime-workspace/programs", "runtime-workspace/data"]
    },
    "repo_files": {"ref": "main", "program": "bridge-bootstrap/repo_files.py"}
  }
}
```

`read_all` is a boolean and allows omitting `data_prefixes`. Without it, the
existing prefix read policy is unchanged. All paths still pass the same
repository-relative path validation; traversal, absolute paths, symlinks and
submodules remain rejected. Snapshot file-count and size limits still apply.
Repository reads never authorize arbitrary Python execution.

`write_all_refs` lists branches allowed to atomically write any normal file in
the repository, including root files, OS, Skill, Story, Fable and assets. Each
entry must also be an allowed ref and a write ref. This example gives main
repository-wide writes; workspace keeps its own narrower grant. This does not
change the execution allowlist, snapshot limits, required `expected_commit`,
unread-file protection or filesystem boundary. Repository OS and Skill workflows
continue to determine when edits are appropriate and how to validate them.

`write_prefixes_by_ref` replaces the legacy shared `write_prefixes` for that
repository. A ref with neither a whole-repository grant nor a prefix grant has
no writes, even if listed in `write_refs`. A ref cannot appear in both
`write_all_refs` and `write_prefixes_by_ref`. Combining a nonempty legacy grant
with either mode is rejected. Each map key must be an allowed ref and write ref.
Existing deployments without `write_all_refs` retain their prefix boundaries.

Optional `repo_files` metadata identifies an existing, allowed canonical Python
program; it grants no additional access and does not create another API. The
discovery result includes its existing `read`, `changes`, `expect` and receipt
contract. Load existing targets in `files`; omit absent paths from that snapshot
list. Per-file SHA-256 guards use `input.expect`, while repository concurrency
still uses `write.expected_commit`. A preflight failure may have outer `ok: true`
with `result.ok: false`, `result.error.code: precondition_failed` and zero changes;
clients must inspect the program result as well as transport success.

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

## Author a program from a Web client

An operator can opt a private, trusted authoring workspace into the same three
MCP tools. Install `examples/workspace_files/main.py` in the target repository as
an allowed canonical program. Give a dedicated branch write permission and make
its program directory both executable and writable. Add this optional discovery
metadata inside that repository's policy (it never grants permissions by itself):

```json
"authoring": {
  "ref": "runtime-bridge/web-workspace",
  "program": "bridge-bootstrap/workspace_files.py",
  "program_prefix": "runtime-workspace/programs",
  "data_prefix": "runtime-workspace/data"
}
```

The branch must occur in the allowed refs and `write_refs`; the helper and program
directory must be under `program_prefixes`; both workspace directories must be
under `write_prefixes`, and the data directory under `data_prefixes`. Invalid
authoring metadata fails configuration validation. This does not enable writes
to the default branch unless that branch is explicitly configured for writes.

1. Discover the authoring workspace with `list_runtime_targets`.
2. Run the helper read-only with `files: []`, `input: {}` to get `source.commit`.
   To inspect existing source, load its path in `files` and pass `input.read`.
3. Have the Web model write ordinary Python with `run(root, input)` returning JSON.
   Save it via the helper and `run_write_skill`, with `input.changes` mapping
   the new `.py` path to source text and the preceding `write.expected_commit`.
4. Run that new path with `run_readonly_skill`; pass all input files explicitly.
   Use `run_write_skill` instead when the program should persist output files.
5. To revise it, load the existing source with the helper, save the edit with a
   fresh commit guard, and execute again. Null changes explicitly delete files.

Program creation and changes require no Bridge rebuild. The helper supplies file
contents, not directory discovery; callers explicitly name the files they need.
The existing trusted-code execution boundary still applies to model-authored
code. This feature does not provide a hostile-code sandbox or dynamic dependency
installation. Programs can return their own structured validation diagnostics;
uncaught runtime errors remain generic to avoid disclosing server details.

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
Explicit-file limits: 64 KiB request, 32 snapshot files, 512 KiB per file,
2 MiB snapshot, 512 KiB result. Directory capacity is documented above;
write transactions retain the original change count and byte caps.

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

## GitHub control plane (0.5.0)

The authenticated MCP server optionally exposes nine additional tools:
`create_github_issue`, `read_github_issue`, `add_github_issue_label`,
`add_github_issue_comment`, `read_github_issue_comments`, `read_github_pr`,
`read_github_pr_review`, `dispatch_local_agent`, `accept_local_agent_result`.
Issue reads include state; PR reads include body/head/base/draft/state. Review
reads include complete changed-file lists, available patches, check runs and
commit statuses, with a second head/base read to reject concurrent changes.
Missing GitHub permissions or truncated data fail closed. Missing patches are
explicitly reported and require source inspection before deciding PASS.

Configure `BRIDGE_GITHUB_CONTROL` as JSON, in addition to existing OAuth/Redis:

```json
{
  "trusted_user_id": "123456",
  "trusted_login": "your-github-login",
  "central_repository": "owner/private-tasks",
  "repositories": {
    "owner/private-tasks": {
      "target_branch": "main",
      "labels": ["documentation", "bug"],
      "modes": ["controlled"],
      "required_sources": ["AGENTS.md"]
    }
  }
}
```

Every repository must also exist in `BRIDGE_REPOSITORIES`; its target branch
must match the runtime policy ref. The numeric control identity must be in the
existing OAuth user-ID allowlist. Every operation verifies that the server's
GitHub token belongs to that exact numeric ID/login and that all involved repos
are private. Supply Issues read/write and Pull requests/checks/commit statuses
read permissions to the existing server credential for the allowlisted repos.
Only the central runner needs contents/PR merge permissions for dispatch.
Tokens remain in server transport; canonical Python retains its empty credential
environment. There is no endpoint/method/token input, settings/secrets/ref API,
workflow editing, repository deletion or merge operation in this control plane.
Ordinary issue tools reject reserved contract/evidence markers; dispatch labels
are only applied by the high-level operations.

`dispatch_local_agent` takes a strict `request` object: `task_repository`,
`target_repository`, short Traditional Chinese `title`, `task`, `allowed_paths`,
`sources`, `validation_profile` (`documentation`, `python-tests`, or
`repository-tests`), `commit_message`, `mode` (`controlled` or `maintainer`),
and `idempotency_key`. Maintainer must be explicitly enabled by policy and uses
repository-tests. The tool creates/read-verifies the existing
`LOCAL_CODING_DISPATCH_REQUEST_V1` Task, adds `local-coding-request`, then creates
a `LOCAL_CODING_DISPATCH_TICKET_V2` control Issue with `local-coding-dispatch` in
the central repo. The receipt includes the exact request contract, Task URL,
number, creation time/status and central ticket URL. The existing GitHub labeled
Issue event starts the self-hosted central workflow. Bridge does not run Codex.

After reviewing the Task intent, trusted terminal evidence, exact PR diff and
validation results, call `accept_local_agent_result` with `request` containing
`task_repository`, `task_issue`, `target_repository`, `expected_commit_sha` and
`expected_outcome: "PASS"`. Bridge verifies the latest trusted terminal PASS,
provider-started/completed, task/target/run/SHA and unique matching ready PR at
the policy base. It creates/read-verifies a `LOCAL_CODING_ACCEPTANCE_V1` control
ticket and labels it for central. Same-repo tickets retain the existing four
fields; cross-repo tickets add `task_repository` (requires the corresponding
central parser update). Bridge never merges or closes. Central independently
revalidates the result, merges with `--match-head-commit`, verifies `MERGED`, and
only then closes the Task. Failure evidence cannot create a PASS ticket.

### Idempotency and recovery

Use one stable key per explicit user request, retaining it across retries.
High-level operations and ordinary Issue creation atomically claim an operation
in the **existing Redis** before GitHub creation. A key cannot change intent.
GitHub Issues carry `LOCAL_AGENT_DISPATCH_RECEIPT_V1` with the hashed operation
key/fingerprint. Retries reconcile all bounded paginated Issues, including closed
ones, verify author/contract, repair an interrupted label step, and return
`existing`. Acceptance derives its key from the task/target/commit, so Work
retries cannot generate duplicate tickets, including after merge/close.

Claims do not expire: a timeout or crash after POST must never cause a duplicate.
`creation_pending_or_indeterminate` means the same call may safely be retried to
find its receipt, but the server will not guess that an uncertain POST failed.
If no receipt exists after an indeterminate creation, operator diagnosis of that
specific claim is required before clearing it; use neither a new key nor a new
Task as a workaround. This is an explicit distributed-transaction failure, not
a queue or polling service. Redis unavailability blocks creation. GitHub API
listing limits block uniqueness claims rather than silently truncating.

Production rollout must separately verify backend discovery, authenticated calls,
ChatGPT's refreshed tool schemas and actual Web tool use. Refresh the existing
app after deploying; keep the same MCP URL and OAuth registration. Native Work
callbacks should validate event repository and explicit `ready_for_review` action
before reading any PR. Other/missing actions exit. The callback reviews via the
Bridge read tools and creates an acceptance ticket only after deciding PASS;
central owns the sole merge implementation.
