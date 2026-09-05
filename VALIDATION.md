# Runtime validation — 2026-09-05

This records successive runtime milestones, including the live ChatGPT Python authoring loop on 2026-09-06. Cloudflare cloud acceptance remains outstanding.

| Case | Evidence | Result |
| --- | --- | --- |
| Core and execution conformance | `python3 -m unittest discover -s tests -v`, 25 tests; inline and isolated execution adapters | PASS |
| Vercel public canonical execution | Production `/api/run`, source commit `7a6a468211335afeae3af80a4ee28b2e1a71db06` | PASS |
| Source update without Bridge redeployment | Same Vercel deployment, source commit changed to `b4a47fccc2cb1c4852311c2a9d5313cc709fa67f`; new `line_count: 130` result field | PASS |
| Vercel HTTP conformance | `tests/http_conformance.py`: auth, path escape, repository/ref/program policy, canonical execution; 6 cases | PASS |
| Cloudflare actual local Worker | Same HTTP suite under workerd/Pyodide, 6 cases, actual GitHub fetch and Python invocation | PASS |
| Cloudflare cloud deployment | Account authentication outstanding | NOT TESTED |
| Hosted private repository | Vercel v0.2, repository-scoped credential, same 6 HTTP cases | PASS |
| Hosted private writes | Dedicated validation branch; 8 live cases listed below, committed bytes independently checked through GitHub | PASS |
| Web agent integration | Action/MCP/API-only entrypoint has not been selected | NOT TESTED |
| Cloudflare actual local Worker writes | Inline adapter covered by core tests; actual Worker write transport not exercised | NOT TESTED |
| Automatic dependency resolution | Explicit file hydration only | NOT IMPLEMENTED |

The Vercel deployment remained
`agent-skill-runtime-bridge-7c9yt7xkw-jies-projects-5abe6c1c.vercel.app`
through the live refresh test; its production alias is
`https://agent-skill-runtime-bridge.vercel.app/api/run`.

Initial probe SHA-256:
`b88725aea2ce2715b2e17c1189808f18a991fbd31a2af4285ddb8adde33948dc`.
Updated probe SHA-256:
`0ac151f39ce41862eef0cb27026d8d8e5667d5dd7b4c96bfb74f682823085967`.
The running Bridge fetched the changed source and returned its changed result.

Cloudflare integration found and corrected three environment/adapter differences:
Node 26 rejected the Pyodide stack-switching flag, so the reproducible invocation
uses isolated Node 22 and uv 0.12.10. Wrangler's module discovery initially omitted
the core when the entrypoint was nested, then over-collected local environments
when moved to the root; an explicit build stage now contains only runtime sources.
The tested Worker fetch rejected `redirect="error"`; `redirect="manual"` plus
rejection of responses other than 200/201 avoids following credential-bearing redirects.

Read-only private-source tests run on a developer machine do not establish hosted
private access. CPython execution-adapter tests do not establish a deployed Worker.
HTTP client tests do not establish agent integration.


## v0.2 hosted write acceptance

Runtime source commit: `4dab73a`. Deployment:
`agent-skill-runtime-bridge-2neb6fm5h-jies-projects-5abe6c1c.vercel.app`.
The private repository credential successfully read canonical Python and submitted
Git tree, commit, and branch updates. Live checks passed:

1. Create two UTF-8 files in one commit.
2. Reject a replay with an outdated expected commit (409).
3. Reject overwriting an existing file not included in the snapshot (409).
4. Reject changes outside the configured write prefix (403).
5. Modify one file and delete another in one commit.
6. Return unchanged HEAD for a no-op, without creating a commit.
7. Two simultaneous requests sharing a base commit yield one success and one
   conflict; only the winning batch reaches the branch.
8. Delete generated test files through the Bridge itself.

Each successful commit was independently checked for its parent, exact changed
path set, and UTF-8 blob contents. Rejected calls left the branch unchanged.
The validation branch retains its audit history; no test data was written to main.
At this milestone, the deployed private policy permitted writes only on that
validation branch and only under its test-data prefix. Production memory-writing policy and
an actual Web agent calling the service remain separate integration work.

After the write changes, the same 6 read HTTP cases passed again on the Vercel
private repository and actual local Cloudflare workerd/Pyodide (public repository).
Cloudflare cloud deployment and actual Worker write transport remain unverified.


## v0.3 MCP and persistent OAuth configuration

The MCP/OAuth adapter with native Redis configuration is deployed to Vercel at
`agent-skill-runtime-bridge-pckp9yk09-jies-projects-5abe6c1c.vercel.app`.
36 local tests pass (25 core/execution plus 11 MCP/OAuth adapter tests).
The ASGI tests cover initialization and metadata, authenticated reads and writes,
read-tool schema separation, policy errors, write conflicts, missing credentials,
strict Origin validation, owner-ID checks, OAuth discovery with S256, and client
registration surviving app recreation using shared encrypted test storage,
native Redis TLS enforcement and explicit configuration precedence.

On 2026-09-06, the OAuth App credentials and a native Upstash Redis integration
were configured in Production. Redis uses the free plan with auto-upgrade,
eviction and Prod Pack disabled. OAuth records use the existing stable encryption
key; the provider-injected `REDIS_URL` connects over TLS.

Live discovery returns 200, anonymous MCP calls return 401 with resource metadata,
and DCR registration returns 201. A registration created on deployment
`agent-skill-runtime-bridge-aq6cpjhdi-jies-projects-5abe6c1c.vercel.app` was accepted
by `/authorize` after redeployment to the deployment above (302 to the consent
page, then 200 HTML with `Cache-Control: no-store`). This verifies production
registration persistence across deployments. The original private
`/api/run` still passes all 6 HTTP cases after the redeployment.

A ChatGPT Projects user has selected this integration route. Developer mode and
the OAuth-only/no-auth/mixed connection choices were confirmed in the account UI;
the form successfully discovers the server's OAuth endpoints and `read:user`
scope, with DCR selected.

## ChatGPT Projects live acceptance, 2026-09-06

Real GitHub profile authorization completed. Refreshing the connected app exposed
all three tools. A new conversation inside the intended ChatGPT Project invoked
`list_runtime_targets` and `run_readonly_skill` against the private canonical
program and two Markdown files. The raw tool response was successful; returned
titles and line counts matched independent GitHub reads at the returned commit.

The same conversation read the allowed validation branch head, created one
dedicated acceptance file with `run_write_skill`, and read it back with the
canonical program. A second guarded write deleted that test file. Independent
GitHub API checks confirmed the exact created contents, each commit's single-file
change, the cleanup branch head, and a final tree identical to the pre-test tree.
Both ChatGPT write confirmations were inspected and allowed once; persistent
permission defaults were not broadened.

Finally, the unchanged runtime (`6e74710`) was redeployed to
`agent-skill-runtime-bridge-58nlqq35u-jies-projects-5abe6c1c.vercel.app`.
The existing ChatGPT connection successfully called the private read tool again
without reconnecting or signing in. This verifies authenticated access across a
real deployment, not expiry-driven token refresh (still untested).

This was an instructed acceptance test, not unprompted tool selection. Formal
Remember/Dream program integration and production memory writes are deferred to
a separate discussion at the user’s request. Cloudflare cloud deployment is still
outstanding.


## ChatGPT creates, saves, reads and executes Python, 2026-09-06

The user clarified the acceptance criterion: Web must generate its own Python,
save it through the Bridge, execute it, and modify and rerun it. Executing a
preinstalled probe alone did not satisfy that criterion.

Runtime `1789e02` adds optional, policy-validated authoring metadata and a small
canonical file helper. The dedicated private branch is
`runtime-bridge/web-workspace`; `runtime-workspace/programs` allows both writes
and execution, with `runtime-workspace/data` for data. Main remains unwritable.
Metadata describes existing permissions and cannot grant additional access.
The 38-test full local suite passed, including actual CPython create/read/run/edit
execution and rejection of inconsistent workspace permissions.

The Web conversation received requirements and test inputs, not Python source.
It generated `runtime-workspace/programs/web_stats.py`, saved it, and executed
that exact source revision. Subsequent requests extended the same program:

| Revision | Change and observed execution |
| --- | --- |
| `4f598437fcc7ac1c3ece4bf906871d55eedfca04` | Web-generated count, total and mean; `[3,7,11,19]` returned 4, 40 and 10 |
| `e12b64207206bd3727d7e22cb3035a4a9556bc4a` | Added min/max; normal input returned 3/19, empty input returned null statistics |
| `aa33bcf24dbff62895bcdeb298b5ee11e0ddaac1` | Read actual source text, added median, saved and executed; median 9 for normal input and null for empty input |

The second iteration initially described loading a file as reading its source,
but inspection of raw tool calls showed no returned source text. This was not
accepted as source-read evidence. Runtime `0bf5ae1` additionally exposes the exact
`input.read` contract and `result.files[path]` evidence requirement directly in
`list_runtime_targets`. All 11 affected MCP tests passed. After deployment to
`agent-skill-runtime-bridge-9u1aekfam-jies-projects-5abe6c1c.vercel.app`, the Web
model rediscovered the contract and actually received the complete second
revision's source before generating the third revision. That raw tool response
was expanded and inspected. Missing initialization instructions in the model's
context are a possible explanation for the earlier mistake, not a proven cause.

Independent GitHub API reads confirmed each commit's sole changed path and exact
source, and the final commit's parent. The final source SHA-256 is
`1c35210b80fb4351fa3063e9c725edf827511858a462ac405b3a78b96edeb64a`.
Both final executions used the third revision and returned:

```json
{"count":4,"total":40,"mean":10.0,"min":3,"max":19,"median":9.0}
{"count":0,"total":0,"mean":null,"min":null,"max":null,"median":null}
```

This establishes the live Web create → save → execute → read source → modify →
save → execute loop. It was instructed acceptance with an intermediate correction,
not unprompted or universally reliable autonomous behavior. The reusable sample
remains on the private workspace branch. Formal Skill program integration was
not part of this acceptance. Execution is for operator-trusted Python, without a
hostile-code sandbox or dynamic package installation. Cloudflare cloud execution
and expiry-driven OAuth refresh remain unverified.

## GitHub control plane, 0.5.0

Full CPython/MCP suite: 101 tests PASS. Ruff `F,E9` checks on all bridge/api/tests
sources PASS; Python compile/import checks PASS. This includes existing OAuth
numeric-ID denial, Redis OAuth persistence across app recreation, program_ref,
historical/directory snapshots, canonical child credential separation and atomic
repository-write regressions. New tests exercise real ASGI MCP discovery/auth,
strict inputs, reserved operations, server credential identity, private repo policy,
dispatch Task/V2 ticket contracts and label readback, repeated/concurrent/ambiguous
creation, Redis atomic durable claims, PR identity/diff/check reads, moving review
snapshots, trusted terminal evidence, acceptance identity mismatches, replay after
merge/close, and creation without any merge/ref API.

The GitHub transport is simulated in these tests. They do not establish hosted
permissions, refreshed ChatGPT schemas, runner event delivery or a zero-relay
production loop; those are recorded separately after actual execution.

Checks API forbidden is reported explicitly with alternative GitHub validation reads; network errors do not trigger this permission-specific path. Required checks with unavailable evidence remain unsatisfied.
