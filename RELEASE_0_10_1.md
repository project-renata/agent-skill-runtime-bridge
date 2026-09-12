# 0.10.1 — bounded GitHub work (pre-release verification)

The previous literal search walked each directory and fetched each candidate blob.
An offline 256-directory/256-file fixture required 515 cold upstream requests,
even with `max_results=1` and no match. This is a demonstrated amplification
defect; it does not establish which caller exhausted the production account quota.

Queries now use a bounded scoped recursive tree, verified small-subtree archives,
a 32-actual-request page budget and signed immutable file/line continuation.
The fixture requires at most four mocked upstream requests (a real archive redirect
adds one download). Warm, expired and independent-process cases remain bounded.
Byte, path, SHA, credential, write and candidate evidence boundaries are retained.

The existing TLS Redis coordinates per-credential admission and provider cooldown.
OAuth shares encrypted successful identities for at most 60 seconds without
stacking SDK TTLs, and reads scopes from the authenticated `/user` response rather
than listing repositories. JWT/JTI, token expiry, owner and scope checks remain.
Provider limits still apply, including usage from other clients/credentials that
share the same account. Coordination failure does not bypass admission or auth.

Validation performed before the subsequent repository-policy bootstrap:

- Full macOS suite: 226 tests, 222 pass, four existing Linux-only skips; no failures.
- Migration suite: 5 pass.
- Wheel and sdist build: pass.
- MCP contract export: 25 tools; generic search adds an optional continuation cursor.
- Cloudflare core staging: pass; its existing portable surface is unchanged.
- Candidate Bridge via local MCP, real GitHub, actual canonical Python:
  continuation entry/decision and recall entry/root-loading all passed at canonical
  `b8015345eed680149054464fcfe6454b9648e2c9`; no repository writes and no 502.
- Real Bridge repository `tests` subtree search: complete, six actual upstream
  requests including ref, commit, prefix tree, recursive tree, archive redirect
  and download. This ran the modified implementation, not only unit-test doubles.

This commit is a prepared infrastructure fix, not a production deployment receipt.
The following bootstrap work will validate and deploy the combined candidate.
No Renata OS program, memory behavior, App identity, endpoint or credential was
changed for this fix. Linux CI and post-deployment checks belong to that release.
