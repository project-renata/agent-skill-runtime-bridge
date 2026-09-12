# Persistent workspace retirement — 0.11.1

Verified on 2026-09-12. This is an infrastructure release record, not an
authoring workflow. Temporary engineering uses a stateless candidate and a
disposable validation environment through the existing repository protocol.

## Cause and production change

Vercel Production's `BRIDGE_REPOSITORIES` retained old execution prefixes,
named refs and per-ref write grants. Changing the repository migration did not
change this independently persisted deployment configuration. The deployed
source was still `80f3820a592a3e1e7acaf2de6ecf0c41d6d0530e`.

The production policy was migrated, verified, and redeployed. Only
`BRIDGE_REPOSITORIES` and `BRIDGE_SOURCE_COMMIT` environment values changed;
credentials and the formal MCP endpoint were preserved. No corresponding
Preview or Development policy copies were configured.

The live `project-renata/project-renata` grant is now:

```json
{
  "ref": "main",
  "program_prefixes": ["memory"],
  "write_refs": ["main"],
  "read_all": true,
  "write_all_refs": ["main"],
  "write_denied_paths": ["runtime-workspace"]
}
```

There are no `additional_refs` or `write_prefixes_by_ref` grants. The remaining
workspace name is an explicit denial of that entire subtree, not authorization.
The Bridge repository's existing engineering policy was preserved, with the new
deployment-policy regression test added to its protected files.

## Source changes

- `maintenance/check_deployment.py`: operator-only, idempotent policy rewrite
  and build preflight. Removes retired path/ref grants and legacy authoring
  metadata; requires the subtree write denial; rejects stale deployment input.
- `vercel.json` / `.vercelignore`: run and include the protected preflight before
  building. `.vercel/static/policy.txt` is an ignored, disposable provider build
  receipt required by Vercel's custom-build output contract. It is not repository
  authoring storage. Python API routing is unchanged.
- `migrations/v0_9_0.py`: reuse retirement when an operator runs the old migration;
  it no longer recreates old ref or write grants.
- `scripts/check_repository_live.py`: replace remote fixture creation/cleanup
  with read → stateless candidate → inspect → validate → unchanged readback.
  It never writes a remote script, creates a branch, or commits a candidate.
- `tests/test_deployment_policy.py`: six regression tests cover stale configs,
  old migration behavior, existing permission enforcement, build wiring and
  stateless validation without repository writes.
- `tests/test_maintenance_policy.py`: test snapshots use tracked files, matching
  the real snapshot; untracked interpreter caches are no longer injected.
- `maintenance/repository-policy.json`: protect the new regression test.
- Version metadata, snapshot expectation, README and historical validation
  documentation updated. No new dependency, tool or generic runtime mechanism.

Implementation commits:

- `db3f0a2fc3bba2b112d625a7ee760217f259891c`
- `ab90bbbbdcf4efd8affc3bf578e4111776100f07`

## Retired data and branches

`project-renata/main` has no `runtime-workspace` tree. This was verified locally
and by live `query_repository` at canonical commit
`9115e8dddefac530853af412537f9286fec572b9`.

Before deleting each retired remote branch, its tree was compared with its merge
base with main. Both had zero net file changes and no workspace tree. Active
canonical code/CI and open PRs had no dependency on either branch. Their unique
histories contained temporary fixtures and their removal.

| Deleted ref | Verified final head |
| --- | --- |
| `runtime-bridge/web-workspace` | `1ddfe303299b151e82a15ab2fee793b483f3ad1c` |
| `runtime-bridge/validation-20260905` | `bf63a1b0615cac693e5b8a4d603d0216917ccfbe` |

Deletion used exact-head leases after production permission revocation.
Subsequent remote enumeration returned neither ref.

## Validation and deployment evidence

- Local full suite: 244 tests, 239 passed and five existing Linux-only skips.
- Historical migration suite: five passed.
- Wheel/sdist 0.11.1 build, exact 25-tool surface export and portable Cloudflare
  staging passed. No Cloudflare deployment was performed.
- Source CI passed: [Infrastructure contract run 34702226310](https://github.com/project-renata/agent-skill-runtime-bridge/actions/runs/34702226310).
- An actual Vercel build with explicitly injected stale repository policy failed
  at the preflight. The clean policy passed the preflight. The local full build
  then encountered the existing Miniforge Python `ensurepip` limitation; the
  actual Linux cloud build completed successfully.
- Production deployment `dpl_6RGDaL7cjZ9ay2QfvPPfadDtyJYo` completed and was
  aliased to the unchanged formal endpoint:
  `https://agent-skill-runtime-bridge.vercel.app/mcp`.
- Live `list_runtime_targets` reports version **0.11.1**, source commit
  **`ab90bbbbdcf4efd8affc3bf578e4111776100f07`**, the grant above, and the same
  25 public tools. This report is a later documentation-only commit.
- Both retired named refs return **403 `repository_or_ref_not_allowed`**.
- Candidate creation at `runtime-workspace/programs/probe.py` and
  `runtime-workspace/data/probe.json` returns **403 `write_path_not_allowed`**.
- Querying the absent `runtime-workspace` tree on canonical main returns
  **404 `repository_entry_not_found`**. A normal canonical candidate inspection
  passes without committing it.
- A candidate against real Bridge source `bridge/repository_query.py` passes
  inspection and `maintenance/validation.json` validation on Vercel Sandbox.
  The protected scope command exits zero, with no findings; the disposable VM
  denies network and child processes. Readback confirms identical repository
  head and file hash. The smoke performs **zero repository writes**.
- Canonical Continuation executes successfully through live MCP, using main
  commit `9115e8dddefac530853af412537f9286fec572b9`. No 502 occurs in these checks.

## Remaining names, not remaining mechanisms

Git history and historical release/validation documents retain old evidence.
The migration, protected preflight, negative tests and explicit write denial
retain the retired names so they can remove or reject them. Historical immutable
reads remain supported. No active workspace data, positive grant, bootstrap
default or remote authoring branch remains, and no replacement workspace was
introduced. Normal canonical memory layout and candidate commit semantics are
unchanged.
