# Bridge 0.10.0：去耦合與通用 repository execution 交付報告

日期：2026-09-12（Asia/Taipei）。本報告記錄已驗證的部署結果，並非未完成的設計提案。

正式環境已部署 **0.10.0**，runtime source 為
`78c61c4aa86cf18813d4b77a649860065a615876`。
部署後真實 query/read → exact candidate → inspect → isolated validation →
commit → retry → readback 已通過。正式 MCP 有 25 個 tools。
本次未使用其他 Agent、派工、GUI 或 browser computer-use。

## 1. Previous decoupling baseline

| 項目 | 結果 |
| --- | --- |
| 原 Bridge base | 0.8.1，`1aef78f1ab6b287481b04ff3fd1929e1914597b1` |
| 第一刀 Bridge 去耦合 | 獨立 commit `d8d8e67b3463dca2489e6ad595096596a300f9ab`，0.9.0 |
| 第一刀 canonical owner migration | `c64f2dbd2933179f0bc75aa1e21b3e00995fd0bf` |
| 第二刀開始時 production | 仍為 0.8.1，`dpl_J2pPmWzS9qm6Vi2iXVYtMGPJ2tTe` |
| 0.9.0 是否單獨部署 | 否；先獨立驗證、commit、push，再從它加入第二刀 |
| 本次對第一刀的處理 | 保留其責任邊界；未復活 tools、aliases、flags 或 workflow handlers |

canonical 專案先安全更新；期間遠端新增的
`3a52e1125f194f884a835131206faceaf8e496e6` 經確認與候選變更不重疊後
fast-forward。那是既有遠端改動，本次第二階段沒有修改 OS、Recall、
Current Self、Story/Fable/Skill 或 executor policy。第二階段 canonical
交付只有 Bridge submodule 指標。

## 2. Architecture

```text
Web / caller：理解、推理、寫程式、選擇 checks、判斷下一步
    ↓ generic calls
Bridge：repository transport、credential、安全執行、SHA 與 receipts
    ↓
canonical world：版本化程式、資料、測試與 validation manifest
```

是否需要某台本機的未提交內容、桌面、硬體、特殊 toolchain 或長時間互動，
由 caller 判斷。Bridge 不知道 Local Codex 是否存在，也沒有 executor 選擇、
fallback、Task、runner 或 acceptance 狀態機。

既有 canonical Python runtime、dependency closure、snapshot、GitHub
resolution 與 atomic writer 保留。新增隔離 validator 是候選程式的安全邊界；
未把既有 trusted canonical runtime 重寫成另一套 runtime。

## 3. New public primitives

| Tool | 責任與重要輸入 | 有界行為 |
| --- | --- | --- |
| `query_repository` | repository、ref、query；tree/search/read | tree 最深 128、32,768 entries；literal search 最多 512 files / 16 MiB、1,000 results；UTF-8 全檔、line range 或精確 byte range；輸出最高 256 KiB |
| `evaluate_repository_candidate` | repository、ref、candidate；operation=inspect/validate；manifest/profile | exact diff、before/after hashes、candidate fingerprint；完整 inspect 與通過的 validation 各發 signed receipt |
| `commit_repository_candidate` | 相同 candidate/fingerprint、message、兩張 receipts | exact base、既有 atomic writer、durable claim、readback；重試只回原 commit，未知結果不盲目再寫 |

三者均不決定任務種類、檢查語意是否充分、何時應執行或應找誰工作。
`POST /api/repository` 是同三個操作的 HTTP adapter，使用既有
`BRIDGE_API_KEY`，共用 service/writer；沒有額外 shell 或 Google/OAuth
管理入口。MCP 使用既有 OAuth 邊界。

## 4. Candidate model

Candidate 是 `repository + ref + base_commit + changes`，不建立長期
workspace/session。每個 create 必須預期檔案不存在；update/delete 必須帶
原檔完整 SHA256，刪除不存在檔案明確失敗。

修改可用完整 UTF-8 replacement，或排序、不重疊的 Unicode code point
range 加 exact expected text；不做 fuzzy patch。最多 32 個 changes、
每檔 512 KiB、changed bytes 2 MiB、JSON request 768 KiB。

Fingerprint 綁 protocol、repository、target ref、base commit、排序後的
path/operation/before/after hashes。不同 edit 表示若產生完全相同 bytes，
fingerprint 相同；不同 bytes、ref、base 或 path 不可借用證據。

inspect 回 exact unified diff，包括 EOF newline。截斷不發 inspection receipt。
validate 使用同一個 base 加相同 overlay，回 fingerprint、manifest hash、
runtime image 與每個 command 結果。完整通過才發 validation receipt。
兩張 HMAC receipt 綁同一 candidate，24 小時有效；host signing key 不出界。
receipt 證明回傳／執行的內容，不能代替 caller 的語意審查。

## 5. Validation execution model

Profile 來自 repository-owned JSON manifest，從同一 base/overlay 讀取。
測試選擇、路徑、相容 Python validator 與 profile 改變，只需 repository commit。

支援固定 CPython 3.14.4、標準函式庫與 snapshot 內相容 Python modules。
只有 `python <repository-script.py>` 或 `python -m <module>`；
沒有 `-c`、shell string、任意 executable、env override 或動態安裝套件。

每次使用現有 Vercel project identity 建立 disposable
[Vercel Sandbox](https://vercel.com/docs/sandbox)，固定 image：

```text
vercel/sandbox/universal@sha256:0e3e3617e824397f170fc7c43ccaa565dd7ac36518e83ead3d41e077cd9f6ec7
```

Provider VM 不持久化、不開 ports、outbound deny-all。VM 內固定 supervisor
建立 readonly chroot snapshot、私人可寫 /tmp，關閉繼承 descriptors，
清理 env、drop uid/gid/groups，再以 seccomp 禁止 exec/fork/clone/network
與其他危險系統呼叫。隔離失敗就不執行，沒有回退到 host execution。

| 限制 | 值 |
| --- | --- |
| Snapshot | 512 files / 16 MiB |
| Commands | 最多 4，依序執行 |
| Validation wall clock | 最多 30 秒 |
| 每 command stdout + stderr | 最多 64 KiB；超限 kill、標 truncated、validation 失敗 |
| Process memory | 512 MiB address space |
| 其他 process limits | CPU、64 descriptors、無 child processes、單一暫存檔 4 MiB、無 core dump |
| Provider VM | 2 vCPU / 4 GiB；hard session limit 120 秒；request 結束銷毀 |

結果明示 exit status、duration、stdout/stderr、timeout、truncated、complete、
resolved commit 與 fingerprint。validation 不把產生的檔案寫回；commit 的仍是
原本 fingerprinted candidate bytes。

pytest、ruff、mypy、npm 與 native dependencies **未預裝**。相容的第三方
Python validators 需在 bounded repository snapshot 提供。
example 只有 unittest 加 syntax/whitespace check，不宣稱是完整 typechecker。

## 6. Security 與 architecture guard

- Repository/ref：沿用 allowlist、read/write/program prefix policy；所有操作回 resolved commit。
- Paths：拒絕 absolute、`..`、backslash、control characters、`.git`；explicit read、snapshot 與 write 拒絕 symlink/submodule。
- Revision：exact base 與逐檔 SHA；moving base、patch mismatch、candidate mismatch 都 fail closed，不 rebase/merge。
- Process：固定 supervisor；readonly chroot、非特權 uid、seccomp、timeout/output/CPU/memory/file limits；關掉輸出管線後掛住也可清除。
- Secrets：host GitHub/Google/Gmail/Redis/OAuth credentials 不傳入 repository process；Sandbox 只收到 snapshot、manifest 與 supervisor。
- Persistence：重用 `GitHub.commit_changes`；單一 tree/commit/ref update；確認 parent、完整 changed-file set 與所有結果 bytes。
- Retry：Redis permanent claim；最多查 32 個 first-parent commits，找相同 base/message/fingerprint 並驗證 bytes。未知結果無證據時回 `commit_pending_or_indeterminate`，不產生第二次提交。
- Evidence：過期、偽造、不完整、錯 candidate 的 receipts 不能提交；簽章不取代 repo permission。
- Architecture tests：鎖定 25-tool surface、tool descriptions/schemas/instructions、metadata、runtime/config 禁止的上層 symbols、credential isolation、新增 canonical program/dependency 無須修改 server、Cloudflare stage 排除 stale runtime。
- 新的 manifest-change test 證明只改 repository 就可改 check selection；沒有硬編任何專案測試或生命週期。

精確協定與維護規則見 [REPOSITORY_PROTOCOL.md](REPOSITORY_PROTOCOL.md)、
[ARCHITECTURE.md](ARCHITECTURE.md)；測試檔是契約的可執行證據。

## 7. Changed files

本次 execution/version commit 共 26 個檔案，1,998 insertions、15 deletions：

```text
ARCHITECTURE.md
README.md
REPOSITORY_PROTOCOL.md
VALIDATION.md
api/repository.py
bridge/__init__.py
bridge/mcp_server.py
bridge/repository.py
bridge/repository_http.py
bridge/repository_models.py
bridge/validation.py
bridge/validation_runner.py
examples/repository-validation/calc.py
examples/repository-validation/check_source.py
examples/repository-validation/tests/test_calc.py
examples/repository-validation/validation.json
pyproject.toml
scripts/check_repository_live.py
scripts/check_validation_live.py
tests/test_architecture.py
tests/test_mcp.py
tests/test_repository.py
tests/test_snapshots.py
tests/test_validation.py
uv.lock
vercel.json
```

部署後 documentation commit 只新增本報告 `RELEASE_0_10_0.md`，更新
`VALIDATION.md` 連結／receipt；不改 runtime，不需要再次部署。
canonical repository 第二階段只更新：

```text
memory/story/projects/Agent Skill Runtime Bridge/bridge
```

## 8. Tests 與真實驗證

以下命令的 cwd 為 Bridge repo，另有註明者除外。

| Exact command / execution | 結果 |
| --- | --- |
| `uv run python -m unittest discover -s tests -q`，macOS | 207 collected；203 pass、4 Linux-only skips；0 failures、0 errors；12.056 秒 |
| 相同命令，Linux CI | 207 pass、0 skips、0 failures、0 errors；19.181 秒 |
| `uv run python -m unittest discover -s migrations -q` | 5 pass；CI 0.159 秒 |
| `uv run python -m unittest discover -s '../../../../skill/integrations/google-services/tests' -q` | canonical Google 8 pass |
| `uv run --with pytest python -m pytest '../../local-coding-dispatch/tests' -q` | 139 pass + 8 subtests pass；無 live dispatch |
| `uv run --with ruff ruff check --select F,E9 bridge api cloudflare scripts tests migrations examples worker.py` | pass |
| `uv run python -m compileall -q bridge api scripts tests` | pass |
| `uv build` | 0.10.0 wheel + sdist pass |
| `uv run python scripts/inspect_surface.py --output /tmp/bridge-0.10.0-surface.json` | 0.10.0、25 tools；完整 schemas/descriptions/metadata |
| 獨立 venv 安裝 wheel 後執行相同 inspector | 0.10.0、25 tools；wheel 有 7 份 Discovery JSON，沒有 retired modules |
| `python3 cloudflare/build.py` 與 stale-stage regression | pass；仍只有既有 portable core |
| `scripts/check_validation_live.py` 經 host-auth wrapper | 6 個真實 pinned Linux microVM security cases pass；全數銷毀 |
| `PYTHONPATH=. uv run python /tmp/bridge-predeploy-smoke.py` | 真實 GitHub + 本機 MCP transport + 真實 microVM，完整 loop 與 cleanup pass |
| `PYTHONPATH=. uv run python /tmp/bridge-postdeploy-smoke.py` | 正式 deployed HTTP adapter + 真實 microVM/GitHub，完整 loop 與 cleanup pass |
| `git diff --check` | pass |

CI：[runtime source 的成功 run](https://github.com/project-renata/agent-skill-runtime-bridge/actions/runs/34671211940)。
前一刀獨立 CI：[0.9.0 run](https://github.com/project-renata/agent-skill-runtime-bridge/actions/runs/34669341446)。

Integration tests 驗證實際 overlay、MCP/HTTP contract、inspect/validate/commit
fingerprint 一致、canonical 在 validation 後尚未變動、exact readback、
concurrent ref 移動、unknown-effect recovery 與 retry after later commit。

Security cases 涵蓋 path/absolute/symlink/submodule、bad ref、stale base/SHA、
patch mismatch、create existing、delete missing、range/truncation、diff EOF、
forged/expired receipts、forbidden executable、shell string、cwd、env、
limits 與 secrets。6 個真實 VM cases 分別為 isolation、CPU timeout、
closed-pipe timeout、oversized output、exit 7、memory bound。
後四類有預期失敗的 validation；測試通過代表它們正確被限制，不是把失敗誤報成功。

### 部署後 smoke receipt

| 證據 | 值 |
| --- | --- |
| Repository/ref | `project-renata/project-renata@runtime-bridge/validation-20260905` |
| 唯一 fixture prefix | `runtime-workspace/programs/validation-2d2b226d045c` |
| Seed/base | `170281ea64cf49c4b1e93842efd3bfeec26365e8` |
| Candidate fingerprint | `b78953ee89b34c3890abaecc0e9ea7654de5da1c1f39b996576f872b143cf45a` |
| Manifest SHA256 | `66443800efdd641f0b9038ca4a1c91f1ef39acf60df17e7a23a68ee17f53ca30` |
| Persisted commit | `1196fa2ddd80e367817ea20cdae21a1b990406a9` |
| Readback SHA256 | `b414e3e8a1cc84d091471727544c353f60db59fc2bea10ee562ece77410915be` |
| Retry | 回傳相同 commit；未重複提交 |
| Validation | unittest 1 pass；syntax/whitespace pass；兩個 exit 0；0.206 秒 execution |
| Cleanup commit | `46eb75bfdc16a73130755994f5dd53a9b707a8c4` |
| Cleanup verification | 四個 fixture files 全部不存在；保留原本既有 integration ref |

predeployment MCP loop 的 persisted commit 為
`a8848bf01f2d9193f0c9eb01869ec57e6efcb635`，cleanup 為
`1c42b46e88bbabc8fd66779385258bbd8c723c61`。兩次 smoke 均未寫入 main。

既有 connector 在部署後成功呼叫 canonical readonly（含 program_ref=main）、
generic GitHub issue read、Google Calendar catalog、Gmail profile。
匿名 `/api/repository` 與 `/mcp` 均回 401；authorization discovery 及
`/.well-known/oauth-protected-resource/mcp` 均回 200。

一次 readonly probe 使用 Bridge repo policy 未允許的 SHA ref，被正確拒絕
`ref_not_allowed`；改用既有允許的 main 成功，source 是部署 commit。
一次 discovery probe 用錯無 /mcp 後綴路徑得到 404，依正式路徑重驗通過。
沒有因此放寬 ref 或 auth policy。歷史 canonical OS fixture failures 記於
VALIDATION.md 的 0.9.0 區段；不是這次 Bridge suite 的 failure，也未順手修改 OS。

## 9. Deployment

| 項目 | 結果 |
| --- | --- |
| Version | 0.10.0 |
| Runtime source | `78c61c4aa86cf18813d4b77a649860065a615876` |
| Production deployment | `dpl_9TYp6wrePt3mqCV1gyRowGJwCQDf`，Ready |
| Immutable URL | https://agent-skill-runtime-bridge-ka45d2pqj-jies-projects-5abe6c1c.vercel.app |
| Active alias | https://agent-skill-runtime-bridge.vercel.app |
| Procedure | `vercel deploy --prod --yes`，既有 project；`vercel inspect agent-skill-runtime-bridge.vercel.app` 確認 |
| Build | Vercel Python 3.12，locked dependencies；api/mcp、api/run、api/repository 三個 functions |
| Live verification | 已授權 MCP `list_runtime_targets` 回 version、source SHA 與 server 實際註冊的 25 tools |

Deployment config 移除 workflow control、specific helper metadata 與 legacy
Gmail fallback；保留 generic repo/ref/path/permission/limit 與 credentials。
新增 source receipt；Vercel Sandbox 使用 provider 已有 OIDC identity。
既有 host service secrets 未暴露給 canonical code，未用空的 masked env export
覆蓋正式 secrets。

一刀 migration 在 promotion 前保存 141 筆 opaque Redis claims、合併本機 encrypted
Google journal；promotion 後再執行一次，剩餘 old claims=0，
journal=already_migrated_or_absent。無遺漏的舊 control namespace 需要長期 runtime
compatibility。

**Jie 最小操作：刷新既有 ChatGPT Bridge app 的 tools 一次。**
本 task 的 connector definitions 仍快取舊 schema/descriptions，尚未載入三個新 tools；
server 實際註冊清單已更新，舊 tools 不存在。目前沒有可呼叫的 connector refresh API，
而本次明確禁止 GUI/browser，所以沒有操作客戶端 Refresh。
既有 OAuth connector 已可成功讀回新 metadata；沒有要求重新部署或重新建立連線。
這項 client cache 更新不能靠在 server 保留舊 endpoint 解決。

## 10. Final public tool surface

以下由 **部署後已授權 MCP `list_runtime_targets.public_tools`** 讀回；
欄位直接由執行中 `mcp.list_tools()` 生成，不是手寫預期清單：

```text
add_github_issue_comment
add_github_issue_label
commit_repository_candidate
create_github_issue
evaluate_repository_candidate
gmail_get_profile
gmail_list_labels
gmail_read_messages
gmail_search_messages
google_mail_compose
google_read_document
google_services_catalog
google_services_execute
google_services_prepare
google_services_read
list_github_issues
list_github_prs
list_runtime_targets
query_repository
read_github_issue
read_github_issue_comments
read_github_pr
read_github_pr_review
run_readonly_skill
run_write_skill
```

`dispatch_local_agent`、`accept_local_agent_result`、
`google_workflow_prepare` 不存在，沒有改名後的等價 orchestration tool。

## 11. Real Web loop

**已具備並在正式 Bridge 跑通** search/read → candidate edit → test →
exact diff → commit → readback；這些 server operations 不依賴 Local Codex、
本機 workspace 或 local runner。smoke 的 host script 只充當 API caller
與隔離 fixture 管理者，candidate validation 實際在正式環境配置的遠端 VM 執行。

證據分層：predeploy 使用真實 MCP transport；postdeploy 使用相同 service
的正式 HTTP adapter；現有 OAuth connector 驗證 active MCP metadata 與既有
tools。未用 GUI 建立新 Web 對話，所以不宣稱已驗收「刷新後 Web 模型自行選擇
新工具」；新工具 client cache 需要上一節的一次 Refresh。

上述完整 loop 適用於支援的 bounded Python 環境。若專案必須跑未提供的 native
toolchain，該 validation 目前不能執行；Bridge 會拒絕，不自行派人或改用 host。

## 12. Deferred／remaining debt

- Native toolchains、預裝第三方 validators、package installation：屬 supported
  execution environment 擴充；本次採最小完整 Python protocol，未建立通用 build farm。
- Regex search：本次使用 bounded literal search 加 path filters，避免新增 regex
  資源風險；沒有缺少基礎 search/read。
- 長時間互動、desktop/hardware、任意 shell、IDE／agent loop：不屬本次 MVP 或 Bridge owner。
- 超過 32 first-parent commits 的不確定提交恢復：有界 reconciliation 之外需 operator
  查證；不以盲目重試交換安全。
- 客戶端 tool schema Refresh：上節已明示尚未操作的唯一使用端步驟。
- Cloudflare cloud acceptance、OAuth expiry-driven refresh 為既有未驗收事項，
  非本次部署或新增執行層的完成依賴；本次未擴建 Cloudflare 架構。

沒有待移出的 runtime workflow、兼容 alias、dispatch mode 或 project config debt。

## 13. Final commits

| 責任 | SHA |
| --- | --- |
| Previous Bridge decoupling | `d8d8e67b3463dca2489e6ad595096596a300f9ab` |
| Previous canonical workflow migration | `c64f2dbd2933179f0bc75aa1e21b3e00995fd0bf` |
| Web execution layer + 0.10.0 version + deployed source | `78c61c4aa86cf18813d4b77a649860065a615876` |

部署後本報告的 documentation commit 與 canonical submodule-pointer commit
另外交付；它們不改 runtime，active source 仍固定在上述 78c61c4。
完整 SHAs 隨本次最終交付訊息列出，Git 保存各自精確 diff。

## Appendix：第一刀 audit、Removed／Migrated／Kept

### Audit 與 Removed

原耦合分成四類：專案 orchestration、generic primitive 的私人政策、
業務 workflow、metadata/description/config 引導。

- 刪除 `bridge/control.py` 的派工／接受 orchestration，刪除
  `bridge/google_workflows.py` 業務流程。
- 正式 tools、handlers、schemas、route registration 移除上述三個 workflow tools。
- 普通 GitHub API 去除 dispatch marker parser、reserved labels、central ticket、
  Local runner identity、acceptance restrictions 與 coding 應派工的 description。
- `BRIDGE_GITHUB_CONTROL` 改成只描述一般權限的 `BRIDGE_GITHUB_API`；
  runtime 不保留舊 control env 或 private workflow schema。
- `list_runtime_targets` 刪除 `github_control`、`repo_files_usage`、
  `authoring_usage`；不再教 caller 用特定 helper 或 workflow。
- 移除 specific helper/Skill path config 與 Gmail legacy credential fallback。
- 清除 dead workflow tests/constants；歷史只留 Git、canonical 記憶與有日期的驗證紀錄。

### Migrated

- Google 的 followup_put、followup_close、registration_track、
  registration_confirm、mail_to_calendar 移到 canonical
  `memory/skill/integrations/google-services/scripts/workflows.py`，
  由 `google-services.py` 入口使用。
- Credential-free resumable host-call protocol 位於 canonical
  `memory/skill/shared/host_calls.py`。workflow 產生 generic
  catalog/read/prepare/execute handoff，caller 把 host result 帶回 canonical Python。
- Dispatch owner 位於 canonical
  `memory/story/projects/local-coding-dispatch/scripts/web_workflow.py` 與
  `orchestration.py`，政策在該專案 `config/web-policy.json`。
  它用 generic GitHub issue/read/label/comment/PR calls；
  專案明確標為 development owner，沒有宣稱新的 live dispatch acceptance。

### Kept／GitHub／Google／metadata

Bridge 保留 canonical readonly/write Python、immutable program/data resolution、
canonical dependencies、bounded snapshot、SHA、safe paths、optimistic atomic
writes、host credentials、generic service transport、limits、caching 與 receipts。

GitHub 正式 surface 是一般 issue/PR list/read、issue create/comment/label 與
PR review；numeric credential identity、repo allowlist/private permission、
bounded complete reads、generic idempotency 保留。讀回含歷史 marker 的 issue
仍當一般內容，不解析為 workflow。

Google/Gmail 保留 catalog/raw read/generic prepare/execute、document read、
MIME compose、mail search/read/profile/labels，credential 始終在 host。
業務定義不影響 server；更改 follow-up 或報名流程只改 canonical owner。

metadata 現在只有可執行 repositories/refs/program/data/write boundaries、
snapshot/transport observations、generic service availability、limits、
version/source receipt、repository protocol 與 actual tool names。

**Maintenance rule：只有 runtime/protocol、transport、安全、credential、
generic service primitive、provider support 或效能／可靠性／limits／caching
等基礎設施改變才值得 Bridge release。上層 OS、Skill、測試選擇、coding、
Google business workflow、executor policy 的改變不觸發 Bridge release。**

