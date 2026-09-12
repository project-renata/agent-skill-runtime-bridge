# Bridge 0.11.0：有界 GitHub transport 與 Web self-maintenance

驗收日期：2026-09-12，Asia/Taipei。結果：**PASS**。

本報告只記錄 Bridge 工程與部署證據，不改 Renata OS、Recall、Continuation
或 canonical 設計記憶。沿用既有 25-tool 與 query → inspect → validate → commit，
沒有新增 agent workflow、dispatch、scope lease 或 deployment tool。

## 根因與修復

### GitHub 請求放大

原先 literal search 先逐目錄走訪，再逐 blob 讀取；max_results 只限制輸出。
256 目錄／256 檔案的離線 fixture，在冷啟動、沒有匹配時需要 515 次 upstream。
OAuth 驗證還會為 scope 檢查列 repositories，跨 instance 缺少共享 admission。

這證明設計有放大問題；不能據此把帳號全部用量歸因於單一 caller。
本輪在部署前再次看見 GitHub primary quota 的 403；等額度重置後才繼續，
沒有連續重試。Canonical 程式無需 workaround。

修正使用 scoped recursive tree、SHA 驗證的有界 subtree archive，以及每頁
最多 32 次實際 upstream request。搜尋回傳完整性、stop_reason、診斷與
簽名 next_cursor；cursor 固定 commit 和檔案／行位置，續頁不重掃先前內容。
達上限可以續頁，不把大 repository 或正常搜尋一律禁止。

同一離線 fixture 降至最多 4 次模擬請求；真實 archive redirect 多一個下載。
正式服務對 tests 子樹做無匹配搜尋：掃描 22 檔、6 次 upstream、complete=true。

既有 TLS Redis 提供每 credential 4 個在途請求、burst 16／每秒補充 8、
provider cooldown 及加密成功 identity cache（最多 60 秒）。
JWT/JTI、scope、owner、到期檢查仍在；Redis 失敗不繞過 admission/auth。
不做自動暴力重試。其他 client/token 仍可能共用 GitHub 帳號額度，
所以這是消除已知放大與協調流量，並非承諾外部服務永不限流。

### Bridge 原本不能維護自己

有兩層原因：

1. BRIDGE_REPOSITORIES 的 Bridge grant 只有 examples 程式與 README.md 資料，
   沒有 read_all、write ref。
2. 正式 Renata-Runtime-Bridge fine-grained token 只選了
   project-renata/project-renata。Bridge 公開庫可讀，但寫入回 github_forbidden。

第一輪 Web 已通過 read／inspect／validate，commit 因第二點遭拒；
readback 確認 main 沒有變更。透過 GitHub 官方 UI，將
project-renata/agent-skill-runtime-bridge 加入同一 token 的指定清單。
沒有重建 token、增加 permission 種類或啟用 all repositories。
原本 canonical grant 完整保留。

最後重新從新的 base 取得 evidence；沒有刪除 durable claim 或繞過安全閉環。

## 最小 policy

Runtime 只增加通用 optional policy：

- write_denied_paths：優先於所有 write grants，atomic writer 也強制套用。
- candidate_only：禁止 run_write_skill／HTTP runner 直接寫入此 repository。
- validation_prefixes：sandbox 可驗證的程式路徑，獨立於 trusted program_prefixes。
- required_validation：固定受保護 manifest/profile；receipt 綁定 host policy，
  替代 profile、過期 policy evidence 都無法通過 commit。

Bridge repo 設 read_all=true、main write_all，普通 source、tests、docs 可形成
candidate；trusted host execution 仍只允許受保護 examples，沒有把全部 source
變成持有 credential 的任意程式執行入口。

精確 operator policy：[maintenance/repository-policy.json](maintenance/repository-policy.json)。
此檔案的 repository 副本不能自行授權；正式權限由 host environment 持有。

受保護範圍：

- maintenance 全部（manifest、checker、scope 與 policy）、.github、api、examples。
- .gitignore、.vercelignore、.python-version、pyproject.toml、uv.lock、pylock.toml、
  vercel.json、wrangler.jsonc、worker.py、cloudflare/build.py。
- bridge/__init__.py、core.py、execution.py、runner.py、mcp_server.py、
  repository.py、repository_http.py、repository_models.py、validation.py、
  validation_runner.py。
- credential／permission 控制：github_coordination.py、github_service.py、http.py、
  gmail.py、google_journal.py、google_services.py、google_discovery、
  google_documents.py、google_local.py。
- 核心 architecture、maintenance policy、repository policy、OAuth、validation
  測試，以及 scripts/inspect_surface.py。

一般 source（例如 repository_query.py、request_budget.py、transport_cache.py）、
其他測試和文件可修改。受保護的混合責任模組需 operator review；
沒有為了擴大可改範圍而重寫成熟安全實作。

## 防漂移檢查

沿既有 sandbox validate，執行 repository 自己的
maintenance/validation.json → maintenance profile → check_scope.py。

受保護 checker 只以 AST／TOML 解析 candidate 資料，不 import candidate。
檢查已核准 top-level 目錄、根檔、檔案類型、Python import、dependency、
frontend manifest、generated/vendor 路徑、檔案數與大小。
Nuxt/Vue 及不相關 dependency 拒絕；文件或測試字串談論它們不會被誤判。

容許 256 檔／8 MiB，普通單檔 512 KiB；既有大型圖示以精確 SHA256 例外保留。
正常 source/tests 成長有餘裕，新增 toolchain、dependency 或 repository 用途需要
operator review。Supervisor 預載 pkgutil，避免 candidate 的同名模組跳過 checker。

這是明顯 scope drift 的機械檢查，不是任意程式正確性或惡意程式的完整證明。
正式 Vercel project 沒有 Git linkage：Web source commit 不會自動替換 credential
host。Infrastructure release 仍需 operator review，但不依赖特定 coding agent。

## 真實 Web E2E

使用 ChatGPT 原有唯一 App：
Agents Skill RunTime Bridge，App ID asdk_app_6aa1678eeb748191afcbbb16213f379b。
原地重新整理後為 25 tools，query schema 有 cursor；沒有建立 replacement App。

[Web 驗收對話](https://chatgpt.com/c/6aa4eaa4-29d0-83ee-8bf8-1e3fcaf41501)

| 驗收 | 實際結果 |
|---|---|
| list_runtime_targets | 0.11.0；Bridge read_all、main write_all、candidate_only 與保護路徑可見 |
| 真實 source read | bridge/repository_query.py；base 80f3820a592a3e1e7acaf2de6ecf0c41d6d0530e |
| inspect | complete=true，diff 僅增加一行註解 |
| validate | maintenance profile，passed=true、complete=true、exit 0 |
| commit | f0bbc9bde4a7d12804e393be48028dde14c0d744，replayed=false |
| immutable readback | readback_verified=true；完整檔案 SHA256 e9c0c7d26dcd9c2ca3dcab0f0796b9d9d6addbdf927e261db83169bb67e6147b |
| Nuxt candidate | passed=false：bridge/unrelated_frontend.py: unapproved import nuxt |
| bridge/core.py candidate | inspect 拒絕：write_path_not_allowed |
| bridge/github_coordination.py candidate | inspect 拒絕：write_path_not_allowed |
| 502／限流 | 最後完整 Web 閉環皆未出現 |

正向 fingerprint：
c493da027dfaf013f12b0364fdc3e9dad4360f650b3588cbf96539e290c4cd49。
新增內容僅為：

```python
# Repository search resumes at an immutable commit via signed cursors.
```

已另從 GitHub fetch 核對真實 commit 與 exact diff。負向 candidate 未提交。

## 回歸與包裝驗證

- 完整 Bridge suite：238 tests；macOS 233 pass、5 Linux-only skips，零失敗。
- 5 個 Linux supervisor tests 另於正式規格真實 microVM 全數通過：
  chroot/uid/seccomp/environment、timeout、output、memory、loader shadow。
- 真實 microVM guard：正常 source pass；Nuxt、pkgutil shadow 均拒絕。
- 真實 Redis acquire/release Lua smoke pass；沒有修改 production identity cache。
- Migration suite：5 pass。
- Canonical 現行 dynamic Recall tests：7 pass。
- Wheel／sdist build、25-tool surface export、Cloudflare staging、diff check 通過。
- 最後 control policy 測試：11 pass；其後 Linux CI 的全套 test/build 通過。
- [Linux CI](https://github.com/project-renata/agent-skill-runtime-bridge/actions/runs/34677256254)
  驗證 source 80f3820a592a3e1e7acaf2de6ecf0c41d6d0530e。

正式 MCP canonical invocation，source
b8015345eed680149054464fcfe6454b9648e2c9：

Continuation entry → decision=recall → Recall entry → 三 root files →
association → stage=complete，全部 ok=true，無 502。沒有修改既有 memory 行為。

正式 project-renata repository engineering 於既有
runtime-bridge/validation-20260905 ref 完成
query → inspect → microVM validate → commit → idempotent replay → immutable readback：

- 成功 commit：2ad390e494a292d3925123c6b639f2e3b3622f35。
- cleanup commit：bf63a1b0615cac693e5b8a4d603d0216917ccfbe。
- 測試 fixture 已逐檔核對後移除，cleanup_verified=true；未修改 canonical main 程式。

## Exact diff 範圍

相對 Bridge 61c41369ac4280d543182a872506cf21abc73480：

- Query/transport：repository_query.py、request_budget.py、github_coordination.py；
  調整 repository.py、http.py、transport_cache.py、repository_http.py、
  repository_models.py、mcp_server.py。
- Policy/runtime：core.py、validation_runner.py；新增 maintenance 四檔。
- Tests：query_efficiency、github_coordination、maintenance_policy；
  更新 OAuth/repository/snapshot/validation 回歸。
- Packaging/version：__init__.py、pyproject.toml、uv.lock、.vercelignore。
  runtime dependency 沒有新增 framework；fakeredis[lua] 僅為測試依賴。
- 文件：README、ARCHITECTURE、REPOSITORY_PROTOCOL、self-maintenance、
  0.10.1 修復紀錄、本報告與有界驗收證據。
- Web 的 source commit 僅一行註解。

Canonical OS／Recall／Continuation／設計記憶沒有改動。

## 部署與 commit

正式 endpoint 保持 https://agent-skill-runtime-bridge.vercel.app/mcp。
已部署 runtime 0.11.0，source
80f3820a592a3e1e7acaf2de6ecf0c41d6d0530e，
deployment dpl_HL2mLqXrQXwPcSecgrHAed1dG3vf。

程式提交：

- 67b535d51678dfbe32febe08fbf3b3a94c2ac30d：請求放大修復。
- 64d8b1024de0a0b3977fc07b7cfb9877ab98f673：0.11.0 bootstrap。
- 80f3820a592a3e1e7acaf2de6ecf0c41d6d0530e：完整 credential 控制檔保護。
- f0bbc9bde4a7d12804e393be48028dde14c0d744：Web 實際 source 提交。

Web 的註解與後續驗收文件是 source repository 的新增提交；沒有自動部署，
所以 source HEAD 與正在服務的 deployment source 不同是預期且可追溯的邊界。
本次沒有待 Jie 手動完成的操作。

## 最終 public tools

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

沒有 dispatch_local_agent、accept_local_agent_result、google_workflow_prepare。
Bridge 升級判準仍是 runtime／transport／credential／security／generic primitive
或 reliability 真的改變；canonical workflow 的演化不需要 Bridge release。
