# Stable infrastructure boundary

Bridge is a canonical Python runtime plus bounded repository transport,
host-owned credentials, generic external-service primitives, safety limits and
receipts. It is not an application's host-side implementation bucket.

```text
Canonical repository (independently versioned programs and data)
        | run(root, input), explicit snapshots, generic tool calls
        v
Bridge (runtime / transport / credentials / safety / receipts)
        | bounded provider API requests
        v
GitHub / Google / other supported providers
```

## Ownership test

Before adding code, schema, metadata, configuration or a tool description, ask:

1. Does this change alter the infrastructure itself?
2. Must it live here because of a host credential, security or transport boundary?

If neither is true, put it in the caller's canonical repository. A service
credential's location does not determine ownership of the caller's workflow.

Bridge owns immutable repository/ref resolution, code loading, declared
dependency closure, separate code/data snapshots, readonly execution, validated
atomic repository writes, optimistic concurrency and source/hash receipts. It
owns provider authentication, fixed API endpoints, native schemas, bounded reads,
write preparation and replay protection. A Google Calendar event and a GitHub
Issue are provider resources; their business meaning belongs to the caller.

Bridge does not own Renata OS stages (Wakeup, Continuation, Recall, Current Self,
Remember, Dream or Sync), memory lifecycle decisions, coding task selection,
Local Agent Dispatch, runner/ticket/acceptance state machines, project labels,
registration confirmation, follow-up definitions or mail-to-calendar decisions.
It does not interpret application markers embedded in Issue/PR bodies. GitHub's
own required checks, pagination and exact-SHA observations remain API facts.

Canonical programs may return a generic tool call and continuation data. The
host invokes that primitive and feeds the actual result back to the program.
This is ordinary JSON on the existing runtime protocol; Bridge does not need a
workflow registry, credential injection, callback interpreter or new endpoint.
The canonical owner validates continuation and evidence, decides ordering, and
retains stable idempotency keys. The host still enforces its API permission and
credential boundaries independently. Caller-provided state is not authorization.

## Release rule

Repository protocol v1 adds three operations and two equivalent transports (MCP
and the existing host API-key trust domain). Queries and candidates are immutable
data; validation intent is repository-owned. Validation execution is an ephemeral
isolated infrastructure service, with no persistent workspace or executor policy.
The original canonical writer remains the only Git write implementation.
See [REPOSITORY_PROTOCOL.md](REPOSITORY_PROTOCOL.md) for the exact boundary.

A Bridge release is justified by a change to:

- runtime protocol or canonical dependency/snapshot transport;
- repository read/write protocol or atomicity;
- security boundary or host credential transport;
- a generic external-service primitive or provider transport support;
- infrastructure performance, reliability, limits, caching or an infra bug.

A new Skill, changed Recall contract, replaced Current Self, removal of Remember,
changed Dream/Sync/coding orchestration, new project GitHub rules, or changed
Google business workflows does **not** justify a Bridge release. Those changes
must be deliverable by updating canonical repository code/data alone. Changing a
real execution/permission boundary still requires operator infrastructure policy
maintenance; do not disguise such a change as a workflow requirement.

## Contract guard

`tests/test_architecture.py` checks the exact fully enabled MCP surface, every
tool description/schema, initialize instructions, runtime metadata, shipped
runtime code and deployment configuration. It rejects known application
symbols. Unknown repository policy fields fail closed. The exact tool allowlist
also blocks renamed orchestration tools; a new generic primitive needs an
explicit architecture review, documentation and tests before the list changes.

The string guard is a regression detector, not a proof of semantic correctness.
Reviewers still apply the ownership test to new generic-looking code. Positive
tests cover independent callers, opaque application text/labels, source changes,
credentials, snapshots, dependency closure, concurrency and atomic writes.

## Upstream work boundary

Output bounds are not upstream work bounds. Query planning must avoid a request
per directory/file, count actual requests independently of returned matches, and
return immutable continuation when the page budget is reached. Existing snapshot
SHA/size/path/credential checks also apply to efficient archive reads. Tests model
cold, warm, expired and independent workers, not only a warm-process happy path.
Deployment-wide Redis admission and cooldown complement process-local object
reuse. A provider throttle must not become an invalid-token refresh loop, and
temporary coordination failure must not remove admission protection or bypass
authentication. These controls belong to infrastructure, independent of callers.

## 0.9 boundary transition

The previous release bundled dispatch and five Google business workflows. Their
implementations now belong to the caller's canonical repository. They have no
registered aliases, hidden routes, wrappers, fallback modes or runtime imports
here. Application file-helper discovery also belongs to the canonical caller;
the same helpers continue to run through the unchanged Python contract.

The one-time operator migration is outside the shipped runtime in
`migrations/v0_9_0.py`. It moves opaque persistent creation claims to the generic
journal namespace and preserves local encrypted Google receipts. These claims
must not be deleted just because old workflow tools disappeared. An old pending
creation may require operator reconciliation; losing a receipt must never make
an uncertain POST eligible for automatic replay.
