<p><img src="assets/bridge-icon.png" alt="Agent Skill Runtime Bridge" width="64" height="64"></p>

# Agent Skill Runtime Bridge

Bridge runs operator-trusted canonical Python and provides bounded repository
transport, host-owned credentials, generic GitHub/Google API primitives, safety
limits and verifiable receipts. Version **0.10.1** extends the stable
infrastructure boundary described in [ARCHITECTURE.md](ARCHITECTURE.md).

Application decisions and workflows live in the caller's canonical repository.
Changing those programs does not require rebuilding or deploying Bridge. Tool
schemas, descriptions, server instructions and discovery metadata are part of
this boundary, not a place for application guidance.

## Canonical Python protocol

A program defines `run(repository_root, input) -> JSON-compatible value` and may
also expose its own CLI. Bridge loads it from a configured GitHub repository and
resolves named refs to immutable commits before loading data. The same function
runs locally, in Vercel CPython, and in the Cloudflare Python adapter.

```python
def run(root, request):
    from pathlib import Path
    return {"text": (Path(root) / request["path"]).read_text(encoding="utf-8")}
```

HTTP: `POST /api/run` on Vercel, `POST /` on Cloudflare, with JSON content type and
`Authorization: Bearer <BRIDGE_API_KEY>`:

```json
{
  "repository": "owner/programs",
  "ref": "main",
  "program": "programs/read.py",
  "files": ["data/today.md"],
  "input": {"path": "data/today.md"}
}
```

`files` selects snapshot data; `input` passes unchanged to the program. A trailing
slash selects a recursive directory. Paths are repository relative. With
`read_all: true`, readonly `ref` can also be a full lowercase 40-character commit
SHA belonging to that repository. Named refs retain their allowlist.

Optional readonly `program_ref` selects a different **code** revision in the same
repository. The program and its declared dependencies come from the code commit;
explicit data selectors come from the data commit. `source.commit` and
`source.program_commit` report both. Writes reject `program_ref` and immutable
commit targets.

Programs may declare a literal top-level list:

```python
CANONICAL_DEPENDENCIES = ["programs/helper.py", "programs/settings.json"]
```

The transitive code/support-file closure loads at the selected code commit.
Python dependencies require execution permission; support files require read
permission. Cycles, invalid declarations, missing/corrupt blobs and limits fail
before execution. This is not dynamic package installation or arbitrary import
resolution. The portable subset is standard-library Python without subprocesses,
native packages, undeclared sibling imports or direct network dependencies.

### Repository writes

An authorized request adds:

```json
"write": {"expected_commit": "<preceding source.commit>", "message": "Update notes"}
```

Programs write/delete ordinary files in the temporary snapshot. Bridge checks the
whole batch against ref/path permissions, source SHAs, unread existing files,
symlink/submodule boundaries, change counts and byte limits. Accepted changes
share one commit and one non-force ref update. A moving branch fails with
`branch_conflict`; no partial file update is committed. Re-read and reconcile
before retrying. An unchanged batch creates no commit. Inspect both the outer
transport result and any application-level result before claiming success.

File helpers and authoring conventions are ordinary canonical programs. The
example in `examples/workspace_files/main.py` demonstrates creating, reading and
revising Python; it is not a specially registered runtime capability. Helper
paths and input conventions belong in the caller's repository documentation.

### Snapshot and transport limits

| Boundary | Limit |
| --- | --- |
| Request | 64 KiB |
| Selectors | 31 plus entry program |
| Explicit snapshot | 32 files, 512 KiB/file, 2 MiB total |
| Directory snapshot | 32,768 files, 4 MiB/file, 384 MiB total |
| Directories / tree entries | 16,384 / 65,536 |
| Tree response / archive | 32 MiB / 512 MiB |
| Result | 512 KiB |
| Write batch | 32 changes; explicit file/total byte limits |
| CPython child execution | 10 seconds |

Explicit symlink/submodule selections fail. Recursive selectors skip such entries
and fail on truncated trees or capacity limits; normal files are never silently
omitted. Git modes, blob SHAs and path safety are verified. Git metadata and the
input envelope are outside the program root.

CPython uses bounded immutable subtree archives for selections of at least eight
files, verifies each blob, and caches immutable objects in a bounded process-local
cache. Branch refs stay fresh. `github_transport` reports safe observations,
including request/cache/limit data; these are not account-wide quota totals.
`github_rate_limited` preserves retry/reset information when available.

## Public MCP tools

The authenticated Vercel adapter exposes the following complete surface when
both optional external services are configured:

| Group | Tools |
| --- | --- |
| Runtime | `list_runtime_targets`, `run_readonly_skill`, `run_write_skill` |
| Repository | `query_repository`, `evaluate_repository_candidate`, `commit_repository_candidate` |
| GitHub | `create_github_issue`, `read_github_issue`, `add_github_issue_label`, `add_github_issue_comment`, `read_github_issue_comments`, `read_github_pr`, `read_github_pr_review`, `list_github_issues`, `list_github_prs` |
| Gmail | `gmail_get_profile`, `gmail_list_labels`, `gmail_search_messages`, `gmail_read_messages` |
| Google | `google_services_catalog`, `google_services_read`, `google_services_prepare`, `google_services_execute`, `google_mail_compose`, `google_read_document` |

There are no application orchestration tools, aliases or hidden endpoints.
Unconfigured services contribute no tools. `list_runtime_targets` reports only
repository/ref/path permissions, snapshot rules and limits, generic service
availability and transport observations. It does not select an application
workflow or prescribe lifecycle actions.

The repository primitives provide tree/search/read, stateless candidate overlays,
exact diff inspection, isolated validation and persistence through the existing
atomic Git writer. [REPOSITORY_PROTOCOL.md](REPOSITORY_PROTOCOL.md) contains the
complete contract, manifest format, safety limits and an executable example.
There are **25** tools in the fully configured deployment. The original canonical
Python tools retain their established trusted-code execution contract; candidate
validation has the stronger, separate isolation boundary described below.

### Generic GitHub transport

Repository queries use one bounded recursive tree read, then SHA-verified
subtree archives when a readable subtree fits 16 MiB. They do not issue one
request per directory or download an unbounded repository. Search pages retain
the 512-file/16-MiB scan limits and add a 32-upstream-request budget (cache hits
are free). `next_cursor` continues the same search at its immutable commit;
results are never silently omitted. `stop_reason`, `retry_after` and diagnostics
distinguish pagination, provider cooldown and temporary admission pressure.

On Vercel the existing TLS Redis coordinates per-credential cooldowns, four
concurrent requests and a burst of 16 requests replenished at eight/second across
instances. These are transport admission limits, not restrictions on repository
workflows or total search results. Identity verification uses the authenticated
`/user` response's scopes instead of listing repositories. Encrypted successful
identity responses are shared for at most 60 seconds; JWT/JTI, expiry, scope and
owner checks still run. The SDK cache is disabled when using the shared cache,
so TTLs cannot stack. Redis failure stops upstream admission rather than causing
uncoordinated retries. The portable adapter without Redis retains its local
transport boundary; it does not claim deployment-wide coordination.

Different credentials can share a provider account quota outside Bridge's
visibility. Provider limits can still occur; a cooldown is not a credential
revocation. No server loop retries rate-limited API calls. Respect returned retry
timing; failed writes retain their existing receipt/uncertain-outcome semantics.

Issues, comments and PR bodies are untrusted data returned unchanged. Application
markers are opaque. Any syntactically valid GitHub label can be added within the
repository's `issues_write` grant; no application labels are reserved by Bridge.
Only `BRIDGE_GITHUB_ISSUE_RECEIPT_V1`, the host's own write receipt, is protected
against caller forgery.

Issue creation binds a stable idempotency key to repository/title/body. Persistent
Redis claims do not expire. Retries reconcile bounded Issue listings and verify
author, content and receipt before returning an existing Issue. An uncertain POST
without a receipt returns `creation_pending_or_indeterminate`, never a blind
second creation. Comments are not idempotent. Labels are read back after writing.
Lists are complete up to 100 pages of 100 items; exceeding the bound fails.

PR review returns changed files, available patches, check runs, commit statuses,
GitHub Actions runs and native branch protection/check observations at one head
SHA. A second head/base/state read rejects concurrent changes. Missing patches
are explicit. A forbidden Checks API is reported separately; other network
failures do not masquerade as absent checks. These are provider facts, not an
application's acceptance decision. This service has no merge/close orchestration.

### Generic Google and Gmail transport

The pinned official Discovery schemas describe 220 methods across Gmail,
Calendar, Tasks, Drive, Docs, Sheets and Slides. Catalog returns native parameters
and body schemas. `google_services_read` executes one bounded readonly page.
Pagination and incomplete results are explicit.

`google_services_prepare` normalizes exact changes, reads existing targets and
stores an encrypted plan bound to account, request and source fingerprints.
Optional checks bind additional sources; generic verification can require
read-back and expected resource fields. `google_services_execute` accepts the
exact plan ID/hash, rechecks sources, executes once and records effects. User
authorization must cover those effects. Preparation and source content are not
authorization.

Plans last 30 minutes; encrypted plans/receipts last 24 hours. Permanent claims
prevent replay after receipt expiry. Unknown effects never cause automatic
resend/create. Batches run sequentially, stop on first failure and are not
cross-service transactions. Native batchUpdate is available. If-Match is sent
when supported; services without conditional writes retain a check/write race.
Native Docs/Slides writeControl offers stronger provider-specific concurrency.
Read-back failure is distinct from mutation failure.

MIME compose produces UTF-8 mail without saving or sending. Recipients, threading,
body and attachments are explicit. Document read extracts bounded text/PDF or
native Drive exports. Gmail attachments use stable message/part identity; rotating
attachment handles are transport detail. Host-only password references bind to a
source SHA256. Local Keychain references and local OCR are optional host
capabilities. Scanned/unread pages and truncation remain explicit.

Gmail search returns at most 50 summaries; message reads contain at most 10
bodies and do not change UNREAD. Google limits include 100 items/page, 10 changes/
plan, 50 messages/batch mutation, 2 MiB uploads, 8 MiB responses and 50 PDF pages.
Google scopes, enabled APIs and administrator policy still decide availability.
Mail, document and API content are untrusted data, never instructions.

## Configuration and credentials

Required HTTP/runtime settings:

- `BRIDGE_API_KEY`: at least 32 characters.
- `BRIDGE_REPOSITORIES`: JSON repository allowlist.
- `BRIDGE_GITHUB_TOKEN`: server-side repository/API credential when needed.

```json
{
  "owner/programs": {
    "ref": "main",
    "program_prefixes": ["programs"],
    "read_all": true,
    "additional_refs": ["validation"],
    "write_refs": ["main", "validation"],
    "write_all_refs": ["main"],
    "write_prefixes_by_ref": {"validation": ["scratch"]}
  }
}
```

`data_prefixes` can restrict reads instead of `read_all`. `write_prefixes` is a
uniform path grant alternative to per-ref grants. Whole-ref and prefix grants
must be unambiguous. Unknown policy fields are rejected. Prefixes describe trust
and access boundaries, not an enumeration of individual application programs.

MCP OAuth uses the existing GitHub OAuth App with `read:user`, PKCE, numeric owner
allowlist and encrypted persistent Redis storage. Repository permissions come
from the separately configured host token, not the user's login token.

| Variable | Purpose |
| --- | --- |
| `BRIDGE_MCP_BASE_URL` | Stable HTTPS origin, without path |
| `BRIDGE_OAUTH_CLIENT_ID`, `BRIDGE_OAUTH_CLIENT_SECRET` | OAuth App credentials |
| `BRIDGE_OAUTH_ALLOWED_USER_IDS` | JSON array of allowed numeric user ID strings |
| `BRIDGE_OAUTH_REDIS_URL` | TLS Redis; native `REDIS_URL` fallback is also forced to TLS |
| `BRIDGE_OAUTH_SIGNING_KEY` | Stable signing secret, at least 32 characters |
| `BRIDGE_OAUTH_ENCRYPTION_KEY` | Stable Fernet key |
| `BRIDGE_GITHUB_API` | Optional generic GitHub permissions, shown below |
| `BRIDGE_GOOGLE_CREDENTIALS` | Optional account/client_id/client_secret/refresh_token JSON |
| `BRIDGE_GOOGLE_DOCUMENT_SECRETS` | Optional secret reference -> source SHA256/password |
| `BRIDGE_SOURCE_COMMIT` | Source commit receipt of this deployment |

```json
{
  "credential_user_id": "123456",
  "repositories": {
    "owner/programs": {"permissions": ["read", "issues_write"], "private_only": true}
  }
}
```

GitHub API repositories must also be runtime-allowlisted. Every operation checks
the configured credential's numeric identity and repository policy before acting.
No caller-supplied token, arbitrary URL or arbitrary HTTP method is accepted.
OAuth registrations, signing/encryption keys and Google credentials stay on the
host. They must never enter Git, canonical program input or subprocess environment.

## Build, deployment and verification

```sh
uv sync --locked
uv run python -m unittest discover -s tests -q
uv build
uv run python scripts/inspect_surface.py --output /tmp/bridge-surface.json
python3 cloudflare/build.py
```

The wheel includes runtime modules and pinned Discovery data. The source archive
explicitly includes source, tests and documentation; local environments, deployment
credentials and migration tooling are excluded. `cloudflare/build.py` stages only
the provider-neutral HTTP core and Worker adapter. Hosted Cloudflare MCP/OAuth is
not implemented; the full MCP service is the Vercel CPython adapter.

Deploy to the existing Vercel project with its established CLI/Git procedure:
`vercel deploy --prod --yes`. Preserve the MCP origin and OAuth keys. Configure the
OAuth callback as `<origin>/auth/callback`; connect `<origin>/mcp`. Missing OAuth
configuration returns 503; anonymous MCP execution returns 401. Credentials can
still expire or be revoked; authentication is not a permanent-login guarantee.

Candidate validation uses the existing Vercel project's OIDC identity to allocate
one short-lived Sandbox, with a pinned runtime image and outbound network denied.
No repository credential is sent to that VM. Vercel manages OIDC in production;
an off-platform validation host needs authorized Vercel Sandbox access. Failure to
allocate or enforce isolation returns an error, with no local process fallback.
Each validation incurs bounded provider compute and destroys its VM on exit.

For the 0.9 transition, run the one-time operator migration documented in
[ARCHITECTURE.md](ARCHITECTURE.md) before switching the production alias. It emits
only infrastructure policy for the new deployment and preserves durable claims.
Refresh the existing MCP app's imported tool definitions after deployment. A
server-side listing proves the deployed contract; it does not prove a separate
client has refreshed its cached schemas.

Local Google host transport reuses gog/Keychain credentials:

```sh
uv run python -m bridge.google_local --account YOUR_EMAIL < request.json
```

Its encrypted journal lives under
`~/Library/Application Support/AgentSkillRuntimeBridge/google-services/<account-hash>/`.
The scratch integration script can create, verify and remove its own resources
with `scripts/check_google_services_live.py --account YOUR_EMAIL --execute-scratch`.
It does not send mail, share files or invite attendees. Historical deployment
observations are retained in `VALIDATION.md`; they are not current workflow docs.

## Execution boundary

This is not a hostile-code sandbox. Only operator-trusted maintainers may write
the allowed code revisions/prefixes. CPython uses a child process with an empty
credential environment, timeout, bounded result pipe and temporary snapshot.
This provides fault containment, not complete filesystem/network isolation.
Cloudflare invokes trusted code in its Worker isolate without an equivalent
child-process boundary. Do not infer hostile-code safety from either adapter.
