# Runtime validation — 2026-09-05

This records the read-only first milestone. The complete MVP is not yet accepted.

| Case | Evidence | Result |
| --- | --- | --- |
| Core and execution conformance | `uv run python -m unittest discover -s tests -v`, CPython 3.12.12, 13 tests | PASS |
| Vercel public canonical execution | Production `/api/run`, source commit `7a6a468211335afeae3af80a4ee28b2e1a71db06` | PASS |
| Source update without Bridge redeployment | Same Vercel deployment, source commit changed to `b4a47fccc2cb1c4852311c2a9d5313cc709fa67f`; new `line_count: 130` result field | PASS |
| Vercel HTTP conformance | `tests/http_conformance.py`: auth, path escape, repository/ref/program policy, canonical execution; 6 cases | PASS |
| Cloudflare actual local Worker | Same HTTP suite under workerd/Pyodide, 6 cases, actual GitHub fetch and Python invocation | PASS |
| Cloudflare cloud deployment | Account authentication outstanding | NOT TESTED |
| Hosted private repository | Dedicated read-only GitHub credential outstanding | NOT TESTED |
| Web agent integration | Action/MCP/API-only entrypoint has not been selected | NOT TESTED |
| GitHub writes and automatic dependency resolution | Outside the implemented first milestone | NOT IMPLEMENTED |

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
rejection of non-200 responses avoids following credential-bearing redirects.

Read-only private-source tests run on a developer machine do not establish hosted
private access. CPython execution-adapter tests do not establish a deployed Worker.
HTTP client tests do not establish agent integration.
