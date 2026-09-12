# Repository engineering access

The previous deployment grant exposed only `examples` and `README.md`, with no
write ref. The operator policy in `maintenance/repository-policy.json` opens full
reads and candidate writes to `main`, including real Bridge source. The operator
installs this policy in `BRIDGE_REPOSITORIES`; the repository copy does not
authorize itself. Other repository grants are unchanged.

Use the existing query → inspect → validate → commit operations. Validation uses
`maintenance/validation.json`, profile `maintenance`. Commit the same fingerprint
with both successful receipts and verify readback. There is no new workflow,
session, scope lease or task controller.

## Protected controls

The generic optional host fields are `write_denied_paths`, `candidate_only`,
`validation_prefixes`, and `required_validation`. They protect the maintenance
guard, CI/deployment configuration and entry points, dependencies/lockfiles,
trusted examples, runtime auth/permission/validation/atomic-write controls, and
core safety tests. Credential-bearing GitHub/Google transports, encryption journals,
provider endpoint schemas and local Keychain adapters are protected too. The exact list is in the operator policy. Ordinary source such
as `bridge/repository_query.py`, request budgeting/caching, other tests and docs
remains editable. Protected changes require operator review, not a self-grant.

The atomic writer applies the same path denial. Direct `run_write_skill` and
`/api/run` writes are rejected for a candidate-only repository. Required receipts
bind the manifest, profile and host policy; stale or alternative-policy evidence
cannot bypass the required check.

Sandbox validation permissions do not expand trusted host execution. Source/tests
load in the isolated VM, while trusted `program_prefixes` remains the protected
`examples` directory. Source access grants neither host credentials nor a deploy
API. The production project is not Git-linked: commits do not automatically
replace the running credential host. Infrastructure releases remain controlled
by the operator after review; this does not require a particular coding agent.

## Scope checks

The protected stdlib-only checker examines the entire candidate snapshot as data;
it does not import candidate modules. It checks Python syntax/import roots,
dependencies, top-level directories/file types, frontend manifests, generated or
vendor paths, and count/byte growth. Nuxt/Vue and unapproved dependencies fail.
Documentation and test strings can still discuss those technologies. The existing
large icon is allowed by its exact SHA256; other files retain the ordinary cap.

Budgets allow 256 files / 8 MiB against a baseline near 100 files / 3 MiB, leaving
room for normal source/tests. New toolchains, dependencies, protected controls or
repository purpose require operator review. The guard's loader preloads its own
stdlib before candidate paths are visible, preventing a shadow module from
skipping the intended script.

These checks prevent obvious drift; they are not a proof of functional correctness
or a malicious-code review. The microVM has Python/stdlib, not the host dependency
environment. Full dependency-backed regressions remain in the existing CI.
