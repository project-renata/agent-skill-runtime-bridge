# Runtime validation — 2026-09-05

This records the v0.2 read/write milestone. The complete MVP is not yet accepted.

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
The deployed private policy currently permits writes only on that validation
branch and only under its test-data prefix. Production memory-writing policy and
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
registration persistence across deployments. It does not verify a real GitHub
sign-in, token refresh, or a completed ChatGPT tool call. The original private
`/api/run` still passes all 6 HTTP cases after the redeployment.

A ChatGPT Projects user has selected this integration route. Developer mode and
the OAuth-only/no-auth/mixed connection choices were confirmed in the account UI;
the prepared form successfully discovers the server's OAuth endpoints and
`read:user` scope, with DCR selected. Creation and access consent remain pending;
actual app linking and tool invocation inside the project remain untested.
