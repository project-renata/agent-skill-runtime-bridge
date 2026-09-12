# Repository protocol v1 — Bridge 0.11.0

Three orthogonal tools expose repository data and bounded execution. They do not
classify tasks, choose an executor, interpret application lifecycles, create
tickets or run autonomous loops. Program/test selection belongs to the repository
and its caller. No server workspace, session identifier or application registry
is created.

MCP and `POST /api/repository` share these operations and the same service/writer.
The HTTP adapter uses the existing `BRIDGE_API_KEY` bearer credential and JSON
`{"operation":"<MCP tool name>","arguments":{...same arguments...}}`. It exposes
only the three repository operations; it does not grant access to Google/Gmail or
OAuth administration. Do not pass that host API key to repository Python.

## Query

`query_repository(repository, ref, query)` resolves a configured ref or permitted
historical commit exactly once. `query.operation` selects:

- `tree`: `prefix`, `max_depth` (1–128), `max_entries` (1–32,768), `max_bytes`.
  Returns path/type/size/Git SHA. Depth/count/output limits mark incomplete results.
- `search`: a case-sensitive, single-line literal `pattern`, optional `prefix`,
  case-sensitive path `glob`/`suffix`, `max_results` (1–1,000), `max_bytes`.
  Returns line numbers and bounded snippets. At most 512 files / 16 MiB are read.
  Large skipped files mark incompleteness; binary files are excluded from text
  search. There is no regex engine, shell expression or local filesystem search.
  Optional `cursor` is the previous `next_cursor`, with other query fields
  unchanged. It binds the repository, policy, query, immutable commit and exact
  file/line position; moving the branch cannot mix revisions between pages.
  Each page spends at most 32 actual upstream requests; cached objects are free.
  A bounded recursive tree replaces directory-by-directory traversal. Eligible
  readable subtrees use existing SHA-verified archives, bounded to 16 MiB of file
  content and 64 MiB compressed/expanded transport. Larger scopes can still be
  searched through continuation; the server does not start an autonomous loop.
  `stop_reason` identifies result/output/scan/request limits or a transient
  provider/admission pause. Honor `retry_after`/`reset_at` when present. A truncated
  provider tree explicitly requires a narrower prefix; it is never reported as
  a complete inventory. Skipped-file counts persist across continuation pages.
- `read`: UTF-8 `path`, optional inclusive 1-based `start_line/end_line` OR exact
  `byte_start/byte_count`. Byte boundaries must align with UTF-8. Whole-file
  SHA256/size remain attached to range reads. `complete` covers the requested
  range; `whole_file` explicitly indicates whether all file content was returned.

Output is capped at 256 KiB, default 32 KiB. Files are bounded to 512 KiB.
Every response includes `resolved_commit`. Repository paths reject absolute paths,
traversal, backslashes, `.git`, control characters and unsafe intermediate entries.
Tree entries identify symlinks/submodules; explicit reads and validation snapshots
reject them. Reads never guess a host checkout path.

## Optional host policy

`write_denied_paths` overrides all write grants for listed paths/subtrees.
`candidate_only` refuses program-produced writes. `required_validation: {manifest,
profile}` requires that exact repository-owned validation entry and binds passing
receipts to current host policy; its manifest must be protected. The operator
also protects the checker/dependencies. `validation_prefixes` grants isolated VM
code loading without widening trusted `program_prefixes`. Omitted fields preserve
existing repository behavior. Caller input cannot change these grants.

Validation directories use bounded recursive listings and SHA-verified subtree
archives. Changed/explicit files retain 512-KiB bounds; unchanged directory files
may use the existing 4-MiB snapshot bound within the 512-file/16-MiB validation
total. Inspection/validation has a 128-actual-request budget; exhaustion issues
no passing receipt.

## Stateless candidate

```json
{
  "base_commit": "<40-character commit>",
  "changes": [{
    "path": "src/calc.py",
    "operation": "update",
    "expected_sha256": "<whole base-file SHA256>",
    "edits": [{"start": 29, "end": 30, "expected": "-", "replacement": "+"}]
  }]
}
```

`create` requires absence (`expected_sha256: null`); `update` and `delete`
require an existing file and its exact SHA256. Delete has no content/edits.
Missing deletion is an error. A non-delete has exactly one representation:

- `content`: exact complete UTF-8 replacement;
- `edits`: sorted, non-overlapping original-text Unicode code point ranges, each
  with exact expected text and replacement. Every hunk matches without fuzz.

Duplicate/overlapping file paths, stale hashes, no-op changes and unsupported Git
entries fail before mutation. Limits: 32 changes, 512 KiB/file, 2 MiB changed
bytes, 768 KiB JSON request. All existing write-ref/path permissions apply.

The SHA256 fingerprint binds protocol version, repository, target ref, base commit,
paths, operation and before/after hashes in canonical order. Equivalent exact edit
representations produce the same fingerprint. An explicitly supplied mismatch
fails closed. No server-side candidate storage is needed.

## Inspect and validate

`evaluate_repository_candidate(..., operation="inspect")` returns exact unified
diffs, explicit EOF-newline changes and before/after hashes. Truncated diffs never
issue inspection evidence. Increase `max_bytes` up to 256 KiB or reduce the batch
if the full diff does not fit. An inspection receipt confirms what was returned,
not that a human or model made a sound semantic judgment.

`operation="validate", manifest="validation.json", profile="tests"` reads the
manifest from the same base with the candidate overlay applied:

```json
{
  "version": 1,
  "profiles": {
    "tests": {
      "files": ["src/", "tests/"],
      "commands": [{
        "executable": "python",
        "argv": ["-m", "unittest", "discover", "-s", "tests"],
        "cwd": ""
      }],
      "timeout_seconds": 10,
      "output_bytes": 16384
    }
  }
}
```

Files, directories and the empty root selector are bounded snapshots. The manifest,
all candidate changes and selected files come from the same immutable base/overlay.
Python files require the repository's configured program-prefix permission.
Only `python` with a repository `.py` file or `-m module` is accepted; no `-c`,
arbitrary executable, shell string, env overrides or dependency installation.
Argument paths and cwd are checked; cwd must exist inside the selected snapshot.

The initial supported environment is pinned CPython 3.14.4 plus its standard
library and repository-supplied Python modules. Tests and repository-specific
lint/type/format checks may be selected by the manifest. Third-party validators
must be compatible Python modules provided in the repository snapshot; pytest,
ruff, mypy, npm and native project dependencies are **not preinstalled**. Changing
a profile, test path or compatible validator source needs only a repository commit.
Adding a native runtime/toolchain is an actual infrastructure change.

Use `examples/repository-validation/validation.json` with profile `tests` to run
the checked-in arithmetic unit test and source checks. The source check is a
small syntax/format example, not a replacement for a full type checker.

## Execution isolation

Each validation allocates a disposable [Vercel Sandbox](https://vercel.com/docs/sandbox)
using the host's Vercel identity. The image is pinned by digest, no ports are
exposed, persistence is disabled, and the provider firewall denies outbound traffic.
The VM receives only snapshot bytes, the declarative profile and Bridge's packaged
supervisor. It receives no GitHub/Google/Gmail/Redis/OAuth secrets or host mounts.

The supervisor creates a chroot containing readonly repository files and the Python
standard library, plus a writable private `/tmp`. It forks before repository code,
closes inherited descriptors, chroots, drops groups/uid/gid to 65534, clears env,
then applies seccomp. Networking, exec, fork/clone, tracing, signaling other
processes and privilege/kernel operations are denied. `/etc`, `/proc`, host paths
and outer VM files are absent. Failure to enforce isolation does not execute code.

Limits: 512 input files / 16 MiB, at most 4 sequential commands, 30 seconds total
validation time, 64 KiB output per command, 512 MiB process address space, bounded
CPU, 64 descriptors, no children, 4 MiB per temporary file. Output overflow kills
the process and marks failure/truncation. Timeout cleanup also handles processes
that close their output pipes. The provider VM has 2 vCPUs / 4 GiB and a hard
120-second session limit; it is destroyed after the request. No interactive or
long-running terminal is available.

Results contain exit status, stdout/stderr, duration, timeout/truncation/completeness,
resolved commit, candidate fingerprint, manifest hash and runtime image receipt.
Only passing complete validation issues signed evidence. No validation-produced
file is persisted; persistence uses the original fingerprinted candidate bytes.
Passing tests establish the chosen checks, not semantic correctness or exhaustive
coverage. A candidate that weakens its own tests remains visible in its exact diff.

## Persist and retry

`commit_repository_candidate` requires the same candidate with its fingerprint,
an explicit commit message, and inspection/validation receipts. Evidence is HMAC
bound to the candidate and expires after 24 hours; the signing key stays on the
host. Receipts are observations, not write authorization.

The existing `GitHub.commit_changes` performs the write: all paths are preflighted,
one Git tree/commit is created, and one fast-forward-only ref update persists the
batch. No competing write pipeline, rebase or merge algorithm is introduced.
The commit carries a generic fingerprint trailer. Readback checks its exact parent,
the complete changed-file set and every resulting byte at the resulting commit.

Permanent Redis claims prevent blind replay after unknown provider outcomes.
A retry searches at most 32 first-parent commits for the exact message/fingerprint
and original parent, verifies all bytes, and returns the original result. An
advanced branch without that receipt fails with `branch_conflict`. An uncertain
claimed write with no observable result fails `commit_pending_or_indeterminate`;
it never creates a second commit automatically. Further reconciliation is an
operator action. No-op candidates and reused evidence for different bytes fail.

## Release boundary

Release Bridge for changes to this generic protocol, isolation/runtime image,
security, credentials, provider transport or infrastructure reliability/limits.
Do not release it because a repository changes its tests, workflow, lifecycle,
business logic, coding approach or executor policy. These remain caller-owned.
