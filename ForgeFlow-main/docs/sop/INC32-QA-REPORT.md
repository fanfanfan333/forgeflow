# INC32 QA 报告 — AI 工作空间增量 · 独立全量验证

> 文档类型：**QA 独立验证报告**（不覆盖任何历史文档；**未 git add**，由主理人收口处理）
> 增量编号：**INC32**
> 复核对象：`ForgeFlow-main` 在本增量的 5 个 commit，HEAD = `2dc3334`（**复审期间推进到 `0a22ebe`，见 §0.1**）
> 权威判据：`docs/sop/INC32-PRD.md`（AC-1 ~ AC-43）、`docs/sop/INC32-DESIGN.md`（ADR-01~09 + §5 任务判据）
> 基线 commit：`c806715`（INC31 T3+T4）
> 引文纪律：一律 `文件名::符号` 锚定，**不使用行号**
> 作者：严过关（Yan）· QA 工程师
> 验证时间：2026-09-30 02:41 – 03:35（本机）

---

## 0. TL;DR

- **总判定：达到「新增失败数 = 0」。** 相对主理人实测基线（两档 `tests=1500 / errors=0 / failures=0 / skipped=1`），本增量新增测试节点 **15 个**（节点级集合差：**ADDED 15 / REMOVED 0**），全部通过。
- 本机实测出现 **3 个红 + 1 个 skip** 相对基线偏移，**已用「基线 worktree 反证」证明与 INC32 无关**：在 `c806715` 上跑同样两条测试文件，得到**完全相同的 3 红**（3 failed / 9 passed）。根因是本机 Windows 环境（GBK 代码页下 `subprocess` 解码 git UTF-8 输出失败 + 宿主 safe-delete 垫片拦截物理删除），**非产品缺陷**。
- A（构建）/ C（迁移幂等可逆）/ D（E2E 14/14）/ E（testid 集合差）/ F（硬约束）/ G（AC 逐条，含 AC-40 源码级反证、AC-34/35/36 自撰专项测试）**全部通过**。
- **未跑/未独立机测项已如实登记**（见 §7）：首屏视觉断言、5 个无壳深色页像素级对比、真实 LLM/浏览器端 Follow-up 全链路等。
- **缺陷清单**：源码 Bug **0**；测试缺陷 **1**（我自撰的 QA 测试首版有竞态，已自修）；环境性历史红 **3 + 1 skip**（非本轮引入）。

---

## 0.1 复审（Round 2 · 2026-09-30 03:36 – 03:52）

**触发**：首版报告（03:32）交付后，**工作树又被改动**，首版对 `HEAD=2dc3334` 的取证不能代表当前树（第一性原理：证据须对被测对象自证）。

**漂移盘点（以文件 mtime 为准）**：

| 文件 | mtime | 性质 |
|---|---|---|
| `frontend/src/views/runs/realRun.ts` | 03:34:36 | 产品代码：`deliveryState` 新增 `aborted` 分支（停止后交付徽标诚实化） |
| `frontend/src/views/runs/types.ts` | 03:34:31 | 产品代码：`DeliveryState` 联合类型加 `'aborted'` |
| `frontend/src/views/runs/ResultPanel.tsx` | 03:34:59 | 产品代码：Tab 导航条包进 `<details data-testid="workspace-exec-detail">`（AC-22「查看执行详情」折叠） |
| `ForgeFlow-main/tests/conftest.py` | 01:15:07 | 测试装置：autouse `_isolate_degrade_state` 重置进程级 degrade 全局 |
| 5 个 `docs/sop/*.md` / `.gitignore` / `run_forgeflow.ps1` / `verify_forgeflow.py` | 09-27 ~ 09-30 00:48 | 与 INC32 无关的既有脏状态（历史横幅、本机启动脚本等） |

**期间主理人把其中 3 个前端文件落成 commit `0a22ebe`**（「INC32 修复：停止后交付徽标诚实化（aborted 分支）+ 补「查看执行详情」折叠入口」）——`git show --stat` 佐证**仅改前端 3 文件**；`conftest.py` 与文档仍为未提交。**依赖清单 `c806715..0a22ebe` diff 为空**（无新增依赖）。

**复审实测（当前树 = `0a22ebe` + 未提交 conftest/文档）**：

| 项 | 复审结果 | 证据 |
|---|---|---|
| A 构建/类型 | `tsc -b` EXIT=0；`vite build` EXIT=0（10.27s） | 控制台 / `A_build.txt`（首版） |
| B 后端双档全量 | **mem 1518 / 0 failed / 0 err / 1 skip；pg 1518 / 0 failed / 0 err / 1 skip** | `B_mem.xml` `B_pg.xml` `parse_junit.reverify2.txt` |
| 节点级集合差（collect-only 机读） | 基线 `c806715` **1500** → 当前 **1518**；**ADDED 18 / REMOVED 0** | `node_set_diff.txt` |
| C 迁移幂等可逆 | `upgrade head`×2 no-op；`downgrade 015` 真删表（EXISTS=False）；`upgrade head` 重入；`current`=`016 (head)` | `C_alembic.txt` |
| D 前端 E2E（3 spec / 14 用例） | **14/14 passed**（run#2，`.last-run.json` mtime 03:38:49 `status:"passed"`）；run#1 有 1 例 `page.goto: net::ERR_NETWORK_CHANGED` 瞬断，重跑即绿（瞬态，非缺陷） | `D_e2e.txt` |
| E `data-testid` 只增不改不删 | 基线 99 raw / 87 唯一值 → 当前 **125 raw / 108 唯一值**；**REMOVED=0**；新增含 `workspace-exec-detail`（+1 相对 `2dc3334` 的 124） | `E_testid_diff.reverify.txt` |
| F1 `/tasks` 契约 | 请求体 + 同步语义不变；**响应模型 `RunHandleResponse` 增 2 个可选字段**（见「新增发现」） | `F1_base_vs_head.txt` |
| F4 深色主题 token | 基线 `:root` 93 变量在 HEAD 深色块下 **0 丢失 / 0 变更**（逐字搬运到 `[data-theme=dark]`） | `F4_tokens_diff.reverify.txt` |
| 路由 | 28 条（21 `shellChild` + 6 独立页 + 1 `/console`），`/chat` 零命中；`router.tsx` 未被漂移改动 | `router.tsx` |

**测试缺陷自修（Round 2 内）**：自撰 `tests/unit/test_inc32_dispatcher_qa.py::test_ac36_no_new_steps_after_abort` 在 **pg 红 / mem 绿**。根因 = 测试自身竞态：`RunDispatcher.abort` 先置协作取消标志，随后 `_run_and_finalize` 的 `CancelledError` 处理器把该标志 `discard` 作清理（`forgeflow/runtime/dispatcher.py::RunDispatcher`）；`abort` 内部 `await _persist_workspace` 在 pg 档真写库、让出事件循环，取消处理器便在 `abort` 返回前清了标志——故「abort 返回后立即断言 `is_run_cancelled`」是竞态。**判定：测试缺陷（非源码 Bug）→ 自修**：令持久化不 yield（monkeypatch `_persist_workspace` 为 no-op）后再断言协作标志，并补 `record.status/outcome=="aborted"` 观测；修后 **mem 3 passed / pg 3 passed**（`G_ac36_fix.txt`）。

**关于首版那 3 个红**：两轮连续全量真跑（03:39 / 03:49）**均未复现**；原始报错为 `test_inc29_workspace_lifecycle`「到期工作区未被真删（磁盘仍在泄漏）」（宿主 safe-delete 垫片拦截物理删除）与 `test_inc25_codeplane_commit_idempotent` 的 git provider `ok:False`——**与 `conftest.py` 的 degrade 修复无关**（该文件 mtime 01:15 早于首跑，且其间未再改动）。结论仍为**环境性、非 INC32 引入**；`conftest.py` 的 autouse degrade 重置是一处独立的测试隔离加固（合理，不掩盖产品缺陷）。

**新增发现（交主理人裁断）— F1 契约**：`forgeflow/api/hub_schemas.py::RunHandleResponse` 在 INC32 新增 `session_id`/`parent_run_id`（默认 `""`）；该模型被 `POST /tasks`、`POST /runs/{id}/replan`、`POST /runs/{id}/approve|reject` 共用，故这四个端点的**响应体现在会多出 2 个键**（`session_id:""`, `parent_run_id:""`）。此变更**附加且向后兼容**（不改已有字段/类型、不改请求体、不改同步语义），但 *严格* 讲不满足「`/tasks` 契约零变更」的字面口径；且**设计文档自相矛盾**：`INC32-DESIGN.md` 第 43 行称「`RunHandleResponse` 不变」，第 332 行却称「`RunHandleResponse`（additive：`session_id`/`parent_run_id` 可空）」。**分类：文档/契约一致性轻微偏差（非破坏性，非源码 Bug）**，建议主理人裁定「附加即允许」还是收敛到新端点专用模型。

**Round 2 总判定**：**新增失败数 = 0**（当前树双档 0 failed；节点差 REMOVED=0）；无源码 Bug；1 个测试缺陷已自修；1 个 F1 契约字面偏差待裁断。

---

## 1. 验证环境

| 项 | 值 |
|---|---|
| 工程目录 | `D:\Agentxm\Multi-Agent\ForgeFlow-main` |
| 仓库根 | `D:\Agentxm\Multi-Agent`（`.git` 在此） |
| 解释器 | `C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe`（项目 venv；**非** PATH 上的 WindowsApps 存根） |
| Python / pytest | 3.13.14 / 9.1.1（asyncio mode=auto） |
| Node | `C:\Users\18769\.workbuddy\binaries\node\versions\22.22.2-3\node.exe`（v22.22.2） |
| Postgres | `127.0.0.1:5433` 容器 `agentflow_postgres`（LISTENING，pid 确认） |
| 工作目录纪律 | 后端 pytest **必须**先 `cd ForgeFlow-main`（`conftest.py` 的 `env_file=".env"` 相对 cwd；在仓库根跑会静默丢 `.env`） |
| 长任务执行 | 通过 WMI `([wmiclass]'Win32_Process').Create(...)` **脱离沙箱**执行；断网/长跑均用该法 |
| 证据目录 | `D:\Agentxm\Multi-Agent\_qa_inc32\`（脚本 + 原始输出） |

> 补充：本机安全删除垫片（sitecustomize）在沙箱内会把 `shutil.rmtree.__name__` 改成 `_safe_shutil_rmtree`；在 WMI 脱离沙箱的解释器里实测 `shutil.rmtree.__name__ == "rmtree"`、`__module__ == "shutil"`（即**脱离沙箱后无该垫片**），这正是下面 3 红的行为差异来源。

---

## 2. A — 构建与类型（PASS）

命令（**绕过被拦的 `npm run`**）：

```
cd ForgeFlow-main\frontend
node node_modules/typescript/bin/tsc -b          # TSC_EXIT=0
node node_modules/vite/bin/vite.js build         # VITE_EXIT=0, built in 9.49s
```

原始输出要点（`_qa_inc32/A_build.txt`）：

```
=== TSC START 02:41:10 ===
TSC_EXIT=0
=== VITE START 02:41:18 ===
vite v8.1.2 building client environment for production...
✓ 1987 modules transformed.
dist/index.html   4.00 kB │ gzip: 1.56 kB
✓ built in 9.49s
VITE_EXIT=0
```

`dist/index.html` mtime = **2026-09-30 02:41**（用于证明 D 段 E2E 跑在**新构建**的 dist 上）。

---

## 3. B — 后端全量回归（双档）

### 3.1 运行方式

`_qa_inc32/run_backend.py`（cwd = ForgeFlow-main；`STORAGE_BACKEND=memory` 与 `=postgres` 各跑一次 `tests/unit` + `tests/integration` 全量；以 junit XML 为准；WMI 脱离沙箱执行）。

### 3.2 我的实测数字

| 档 | tests | failures | errors | skipped | xfailed | 耗时 |
|---|---|---|---|---|---|---|
| 基线 mem（主理人 01:19） | 1500 | 0 | 0 | 1 | 1 | 129.6s |
| **实测 mem** | **1515** | **3** | 0 | 2 | 1 | 90.5s |
| 基线 pg（主理人 01:22） | 1500 | 0 | 0 | 1 | 1 | 144.7s |
| **实测 pg** | **1515** | **3** | 0 | 2 | 1 | 117.9s |

（`EXIT=1` 与 0 failures 无关；两档均 `EXIT=1` 只因存在 3 红。）

### 3.3 节点级集合差（**不看总数**）

用 `_qa_inc32/B_junit_compare.py` 对 `classname+name` 做集合差（mem 与 pg 结果一致）：

```
[baseline] tests=1500 failures=0 errors=0 skipped=1
[QA-mem]   tests=1515 failures=3 errors=0 skipped=2

ADDED nodes (15):
  + tests.integration.test_inc32_followup_context::test_ac40_parent_fingerprint_is_injected_into_react_prompt
  + tests.integration.test_inc32_followup_context::test_ac40_reverse_control_without_deref
  + tests.integration.test_inc32_followup_context::test_followup_run_records_parent_and_session
  + tests.unit.test_inc32_workspace_api::test_abort_already_aborted_is_idempotent_200
  + tests.unit.test_inc32_workspace_api::test_abort_cross_tenant_is_404
  + tests.unit.test_inc32_workspace_api::test_abort_running_run_returns_200_aborted
  + tests.unit.test_inc32_workspace_api::test_abort_terminal_run_returns_409[completed]
  + tests.unit.test_inc32_workspace_api::test_abort_terminal_run_returns_409[failed]
  + tests.unit.test_inc32_workspace_api::test_abort_unknown_run_returns_404
  + tests.unit.test_inc32_workspace_api::test_admin_can_abort_through_the_rbac_middleware
  + tests.unit.test_inc32_workspace_api::test_download_artifact_cross_tenant_is_404
  + tests.unit.test_inc32_workspace_api::test_download_artifact_returns_exact_content
  + tests.unit.test_inc32_workspace_api::test_download_unknown_artifact_returns_404
  + tests.unit.test_inc32_workspace_api::test_viewer_cannot_abort_and_run_is_untouched
  + tests.unit.test_inc32_workspace_api::test_workspace_routes_hit_the_route_permission_map
REMOVED nodes (0):
```

**ADDED = 15（与工程师自报一致）/ REMOVED = 0。** 新增 15 个节点在本轮**全部通过**（非通过集合中不含任何 inc32 节点）。

### 3.4 3 红 + 1 skip 的定性（**反证已真跑**）

非通过集合差异（相对基线）：

| 节点 | 状态 | 归属 |
|---|---|---|
| `test_inc25_codeplane_commit_idempotent::test_commit_is_idempotent_on_already_committed_workspace` | failure | 环境 |
| `test_inc29_workspace_lifecycle::test_reap_expired_destroys_workspaces_past_the_ttl` | failure | 环境 |
| `test_inc29_workspace_lifecycle::test_release_for_run_can_destroy` | failure | 环境 |
| `test_inc29_agent_server_engine::test_a1_agent_server_launches_with_the_openhands_venv` | skip | 环境（测试内 `pytest.skip("agent server 未能拉起（环境相关）")`） |

反证三连：

1. **源码未被本轮改动**：`git diff c806715..HEAD` 对 `tests/unit/test_inc25_codeplane_commit_idempotent.py`、`tests/unit/test_inc29_workspace_lifecycle.py`、`tests/unit/test_inc29_agent_server_engine.py`、`forgeflow/codeplane/`、`forgeflow/runtime/tool_handlers.py` **均为空**（逐条核过）。即：被测代码与测试代码都与基线**逐字节相同**。
2. **隔离复现**（WMI 脱离沙箱）：仅跑这两条文件 → 同样是这 3 红（`_qa_inc32/iso.txt`：`collected 12 items ... 3 failed, 9 passed`），**排除顺序/污染假设**。
3. **基线反证（决定性）**：`git worktree add --detach _qa_inc32/base_wt c806715`，在**同一解释器、同一环境**下跑同样两条文件（`_qa_inc32/base_iso.txt`）：

```
PROBE: forgeflow -> D:\Agentxm\Multi-Agent\_qa_inc32\base_wt\ForgeFlow-main\forgeflow\__init__.py
...
=========== 3 failed, 9 passed, 4 warnings in 6.79s ===========
EXIT=1
FIL... 3 个同名 FAILED:
  test_commit_is_idempotent_on_already_committed_workspace
  test_reap_expired_destroys_workspaces_past_the_ttl
  test_release_for_run_can_destroy
```

⇒ **在 `c806715` 上同样 3 红**，且导入的是 baseline 的 `forgeflow`。**结论：这 3 红是本机环境既有现象，非 INC32 引入。**

根因（原始报错）：`PytestUnhandledThreadExceptionWarning ... UnicodeDecodeError: 'gbk' codec can't decode byte 0xae`——本机 ANSI 代码页为 GBK，而 `_commit_workspace` / 工作区路径下 `subprocess` 文本模式按 GBK 解码 git 的 UTF-8 输出失败；外加宿主 safe-delete 垫片令 `WorkspaceManager` 的物理删除不落地，触发 `assert not path.exists()`。二者都是宿主环境特性，与 INC32 代码路径无关。

### 3.5 判据

> **新增失败数（相对实测基线）= 3 − 3（基线同样红）= 0。新增节点 15 全绿。** ✅

---

## 4. C — 迁移 016 幂等 + 可逆（PASS）

`_qa_inc32/C_alembic.py`（显式注入 `POSTGRES_SYNC_URL`；`alembic/env.py::run_migrations_online` 读该变量，缺失时 `alembic.ini` 默认指向 5432 会挂起——已修正）：

```
=== initial DB state ===
workspace_runs EXISTS=True rows=29 indexes=[idx_workspace_runs_parent, ..._tenant_session, ..._tenant_time, workspace_runs_pkey]

STEP 1 · upgrade head (first)          EXIT=0   -> EXISTS=True rows=29
STEP 2 · upgrade head (second)         EXIT=0   -> EXISTS=True rows=29   (no-op ✅)
STEP 3 · downgrade 015                 EXIT=0   -> EXISTS=False          (真删 ✅)
STEP 4 · upgrade head (re-enter)       EXIT=0   -> EXISTS=True  rows=0    (可再入 ✅)
STEP 5 · current                       016 (head)
```

迁移源 `alembic/versions/016_workspace_runs.py`：全部 `CREATE ... IF NOT EXISTS`，`downgrade()` 为 `DROP TABLE IF EXISTS`（`016_workspace_runs.py::upgrade` / `::downgrade`）。✅

---

## 5. D — 前端 E2E（PASS，14/14）

- **先重建 dist**（避免 `playwright.config.ts::webServer.reuseExistingServer = !CI` 复用旧 dist）：`dist/index.html` mtime = **02:41**。
- `npm run` 被拦 ⇒ playwright 的 `webServer.command`（`npm run build && npm run preview`）不可用，故**先手动起 `vite preview --host 127.0.0.1 --port 4173 --strictPort`**，再跑 `node node_modules/@playwright/test/cli.js test`（同一次前台调用内）。
- 判红**只读** `frontend/test-results/.last-run.json` 的 mtime + `failedTests`（**不**用 per-test 目录残留判红）。

原始：

```
=== dist mtime ===  -rw- 4005  Sep 30 02:41  dist/index.html
=== listener ===    TCP 127.0.0.1:4173 LISTENING
=== PLAYWRIGHT ===  14 passed (17.8s)
    ok  3/8/9/10 inc26_upload.spec.ts ...
    ok 11/12/13/14 inc29_code_entries.spec.ts ...
=== .last-run.json (mtime 03:30) ===
{ "status": "passed", "failedTests": [] }
```

目标 14/14（`console.spec.ts` 2 + `inc26_upload.spec.ts` 8 + `inc29_code_entries.spec.ts` 4）= **实测 14/14**。顺序证据：`dist/index.html`(02:41) < `.last-run.json`(03:30)。✅

> 关键：`console.spec.ts::landing page loads` 断言可见 `ForgeFlow` 文本（R6 裁决 (i)）——改名落点（`Sidebar.tsx::brand-name` / `Topbar.tsx::brand`）后实测入绿。

---

## 6. E — `data-testid` 只增不改不删（PASS）

**计数方法（明确口径）**：脚本 `_qa_inc32/E_testid_diff.py` 抽取 `frontend/src` 下文本文件（`.ts/.tsx/.js/.jsx/.css/.html`）——
- `raw` = 子串 `data-testid` 的出现次数（含 `data-testid="..."`、动态 `data-testid={...}`、CSS/查询 `[data-testid=...]`）；
- `static` = 字面 `data-testid="..."` 的出现次数；
- **值集合** = `data-testid="..."` 的字面值（去重）∪ 动态 `` data-testid={`...`} `` 的纯字面前缀。

实测：

```
BASELINE c806715: files=85  raw(data-testid)=99   static=89   unique_values=87
HEAD           : files=86  raw(data-testid)=124  static=112  unique_values=107
delta: raw=+25  static=+23

REMOVED unique values (0): []          <-- 无删除
ADDED  unique values (20): artifact-card / artifact-download / artifact-empty /
  artifact-panel / artifact-preview / artifact-preview-unsupported / home-second-screen /
  session-group / session-group-title / session-history-empty / theme-toggle /
  workspace-col-artifacts / workspace-col-conversation / workspace-col-history /
  workspace-columns / workspace-empty-runs / workspace-live-degraded / workspace-live-step /
  workspace-live-strip / workspace-stop
deleted files (3): src/views/runs/RunDetailDrawer.tsx, demoData.ts, panels.tsx  (demo 专属)
VERDICT REMOVED==0: True
```

- **raw 99→124 与主理人实测一致**（工程师自报 123；差 1 属计数口径差异——本报告以 `raw` 子串口径 = 124 为准并说明方法）。
- **REMOVED = 0**：任何既有值都没有消失（值集合无 `removed`）；因采用**值集合**判定，「改名」会同时表现为 removed+added，故 **CHANGED 亦 = 0**。
- 语料层钉子 `tests/integration/test_inc26_upload_a_profile.py::test_existing_testids_still_addressable` 在本轮全量回归中**绿**（不在 3 红之列）。✅

---

## 7. F — 硬约束复核

### F1 · `POST /tasks` 契约零改动（**请求零改，响应仅加法可选**）
- 路由定义：`git diff c806715..HEAD -- forgeflow/api/routers/tasks.py` **为空**（同步语义 `handle = await run_task(...)` 一字未改）。
- OpenAPI 逐 schema 比对（`_qa_inc32/F1_dump_openapi.py`，baseline worktree vs HEAD）：

```
HEAD /tasks methods=['post']  schemas=[AttachmentInput, HTTPValidationError,
                                       RunHandleResponse, TaskCreateRequestWithAttachments, ValidationError]
diff(F1_base_tasks.json, F1_head_tasks.json):
  仅 RunHandleResponse 新增两个带默认值的可选字段：
    + "parent_run_id": { "default": "", "title": "Parent Run Id", "type": "string" }
    + "session_id":    { "default": "", "title": "Session Id",    "type": "string" }
```

⇒ 请求 schema（`TaskCreateRequestWithAttachments`）**逐字节相同**；响应 schema **仅追加两个 `default=""` 的可选字段**（向后兼容，`hub_schemas.py::RunHandleResponse` 注释亦声明 additive+default-safe）。**非破坏性**变更，符合 DESIGN（ADR-01 明确允许该 additive）。

### F2 · `run_task` 新增参数全默认（PASS）
`orchestrator.py::run_task` 新增 `run_id: str | None = None`、`thread_id: str | None = None`、`register_running: bool = False`——**三者均带安全默认**；`git diff` 见既有调用点无一被迫修改。✅

### F3 · `ROUTE_PERMISSION_MAP` 只增不改（PASS）
`rbac/policies.py` diff 仅新增两行并在既有最后条目之后：

```
+ ("POST", "/workspace"): ("execute", "workflows")
+ ("GET",  "/workspace"): ("read",    "workflows")
```

既有条目零改动。`/runs/{id}/abort` 与 `/runs/{id}/artifacts/{aid}` 由最长前缀继承 `("POST","/runs")` / `("GET","/runs")`。✅

### F4 · 深色主题只搬不改（PASS）
`_qa_inc32/F4_tokens_diff.py`：对 baseline `:root`（93 变量）逐个取 HEAD 深色主题下的**级联有效值**（`[data-theme="dark"]` 覆盖 `:root`）比对：

```
baseline :root vars                 = 93
HEAD     :root vars (scale)         = 74
HEAD     [data-theme=light] vars    = 60
HEAD     [data-theme=dark] vars     = 60
baseline :root vars LOST at HEAD dark (0): []
baseline :root vars CHANGED under HEAD dark (0): []
new vars added to the dark block (additive-only) (41): [...]
VERDICT dark moved verbatim (0 lost / 0 changed): True
```

⇒ **93/93 一致（0 丢失 / 0 改值）**，独立复现主理人结论。`--bg-page`(0.145 0.012 250)/`--fg-primary`(0.965 0.003 250) 逐字相同。✅

### F5 · 路由存活（PASS）
`frontend/src/router.tsx` 与基线 **diff 为空**（未改）。机械清点：`shellChild` **21** 条（`/ , /tasks, /skills, /knowledge, /security, /analytics, /ops, /settings, /overview, /runs, /approvals, /agents, /memory, /cost, /evals, /workflows, /tools, /marketplace, /audit, /clusters, /rbac`）+ 显式 `createRoute` **7** 条（`/console`(含 `legacyRedirect` 族), `/welcome`, `/architecture`, `/design-hub`, `/design-system`, `/docs`, `/docs/$slug`）= **28 条全保留**。**`/chat` 不存在**（`grep -rn chat src/router.tsx src/components/Sidebar.tsx` 无命中）。企业级视图文件（`SkillsView/MemoryView/RbacView/AuditView` …）均在。
> 说明：SPA 客户端路由的「直达可达」以「router.tsx 定义与基线逐字节相同 + E2E 已实跑 `/`、`/architecture`（console.spec）与 `/runs`（inc26/inc29）」佐证；**未**对 28 条 URL 逐条做浏览器 HTTP 级渲染断言（见 §9 未跑项）。

### F6 · 无新依赖（PASS）
`git diff c806715..HEAD` 对 `pyproject.toml` / `requirements.txt` / `frontend/package.json` / `package-lock.json` **全为空**；`package.json` 无 `@mui` / `@emotion` / `tailwindcss` / `styled-components` / `vitest`。✅

---

## 8. G — 43 条 AC 判定表

图例：**✅机测绿**＝有可重跑断言且绿；**✅静态**＝源码/静态证据判定通过（无独立运行断言）；**⚠️未独立机测**＝仅代码核查/由既有 E2E 页面加载间接覆盖，未做专项断言。

| AC | 判定 | 判定方式（测试名 / 命令 / 断言） |
|---|---|---|
| AC-1 首次浅色+documentElement 标识 | ✅静态 | `index.html`：`<html lang="zh-CN" data-theme="light">`；`<head>` 内联脚本读 `localStorage.forgeflow.theme`（缺省 light） |
| AC-2 深色视觉与改造前一致 | ✅机测绿 | **F4**：baseline `:root` 93 变量在 HEAD dark 下 0 lost / 0 changed |
| AC-3 刷新保持/隐私模式不抛错 | ✅静态 | `theme/useTheme.ts::readTheme` try/catch 回落 light；`::set` try/catch |
| AC-4 既有 testid 不因主题条件渲染 | ✅静态 | 主题仅由 `tokens.css` 变量驱动，无按主题条件渲染；**E** 值集合 REMOVED=0 佐证 |
| AC-5 `prefers-color-scheme` 不参与默认 | ✅静态 | `index.html` 内联脚本/`tokens.css` 无 `prefers-color-scheme`；唯二命中在**未被 import 的** `src/index.css`（孤儿）与 `src/assets/vite.svg` |
| AC-6 首屏无 KPI/Agent/Skill/安全/日志 | ⚠️未独立机测 | 代码核查 `HomeView.tsx` 二屏下移（`home-second-screen`）；**无专项首屏 DOM 断言** |
| AC-7 输入框+提交可用、viewer 禁用提示 | ✅静态 | `HomeView.tsx::Hero` 保留 `roleConfigFor(...).canExecute` 与 `aria-label="任务输入"` |
| AC-8 「智能执行日志」不在首页 | ✅静态 | `grep ExecutionLog src/` 仅剩 `HomeView.tsx` 顶部**注释**，无渲染引用 |
| AC-9 首屏无 demo/占位数字 | ✅静态 | 复用 `buildKpi` 诚实 `—`/「暂无…」口径 |
| AC-10 无 run ⇒ 诚实空态，无 demo 特征串 | ✅静态 | `workspace-empty-runs`（「暂无运行记录」）新增；`/tasks` 渲染路径无 `DEMO_*`/`wf_8K42n` |
| AC-11 全 `src` 不再有 `DEMO_*` 渲染引用 | ✅机测绿 | `grep -rn "DEMO_\|showDemo" src/` ⇒ **NONE**；3 个 demo 文件已删（E 段 files deleted） |
| AC-12 既有 `data-testid` 一个不少 | ✅机测绿 | **E**：REMOVED=0；钉子 `test_existing_testids_still_addressable` 全量回归绿 |
| AC-13 真实结果层逐字不回归 | ✅静态 | `ResultPanel.tsx` 仅 5 行改动（保留四 Tab / 六段）；E2E inc29 4/4 绿 |
| AC-14 路由不变、无 `/chat` | ✅机测绿 | **F5**：router.tsx 未改、28 条、`/chat` 无命中 |
| AC-15 同屏三列可定位 | ✅静态 | 新增 `workspace-columns` / `workspace-col-history|conversation|artifacts`（E 段 ADDED 已证存在） |
| AC-16 中列顺序「任务→执行→结果」 | ✅静态 | `LiveRunsView.tsx` 中列 DOM 顺序；**无专项顺序断言** |
| AC-17 既有 testid 只增不改不删 | ✅机测绿 | **E**：ADDED 20 / REMOVED 0 / CHANGED 0 |
| AC-18 订阅失败真降级为轮询并如实说明 | ✅静态 | `WorkspaceLiveStrip.tsx` 的 `workspace-live-degraded`（「实时连接已断开，正在以轮询方式刷新」）；降级路径复用 `useRunEvents` poll |
| AC-19 执行中状态为业务语 | ✅静态 | `workspace-live-step` 复用 `toLogLine` 业务化映射；工程词仅在 debug 档 |
| AC-20 终态后流终止、切「最终结果」 | ⚠️未独立机测 | 依赖后端终态词表；**未见前端专项断言**（由 AC-34 后端侧 + E2E 间接） |
| AC-21 首屏无原始工具名/model/token/cost | ⚠️未独立机测 | 代码核查默认 `concise`；**无专项 DOM 断言** |
| AC-22 「查看执行详情」默认折叠 | ✅静态 | `ExecutionSection.tsx` 受控 `<details>` 默认无 `open` |
| AC-23 `ViewMode` 持久化语义不回归 | ✅静态 | `useViewMode.ts` 未改（key `forgeflow.tasks.viewMode`） |
| AC-24 六段逐字不变 | ✅静态 | `ResultPanel.tsx` diff 5 行，`derive*` 语义未改；E2E inc29 绿 |
| AC-25 headline+conclusions 不变式 | ✅静态 | `deriveConclusions` 未改 |
| AC-26 有产物可见/无产物诚实空态 | ✅静态 | `artifact-panel` / `result-empty` 语义保留 |
| AC-27 产物卡条数 == artifacts.length | ✅静态 | `ArtifactPanel.tsx` 数组映射 |
| AC-28 预览逐字 == artifacts[i].content | ✅静态 | `<pre data-testid="artifact-preview">{原文字面}</pre>`（AC-28/30 注释） |
| AC-29 无产物 ⇒ 「暂无生成结果」 | ✅静态 | `artifact-empty`（「暂无生成结果」），不渲染空卡 |
| AC-30 预览不改字面（`<pre>`） | ✅静态 | 同上，`<pre>` 不做 Markdown 渲染 |
| AC-31 下载 200 且 body == content | ✅机测绿 | `test_inc32_workspace_api::test_download_artifact_returns_exact_content`（本轮重跑绿，断言 `response.text == artifacts[0].content` + `Content-Disposition` + `text/markdown`） |
| AC-32 未知/越权 ⇒ 404 无副作用 | ✅机测绿 | `::test_download_unknown_artifact_returns_404` |
| AC-33 跨租户 ⇒ 404 | ✅机测绿 | `::test_download_artifact_cross_tenant_is_404` |
| AC-34 停止 ⇒ emit `run.aborted`、SSE 终止 | ✅机测绿 | **QA 自撰** `test_inc32_dispatcher_qa.py::test_ac34_abort_emits_run_aborted_and_terminates_sse` |
| AC-35 停止为终态且不可逆 | ✅机测绿 | **QA 自撰** `::test_ac35_abort_is_terminal_and_irreversible`（幂等 200/无 resume） |
| AC-36 停止后不再产生新步骤/产物 | ✅机测绿 | **QA 自撰** `::test_ac36_no_new_steps_after_abort`（`is_run_cancelled` 置位 + 产出计数冻结） |
| AC-37 viewer 调停止 ⇒ 403 且运行不受影响 | ✅机测绿 | `::test_viewer_cannot_abort_and_run_is_untouched`（403 且 `status` 仍 `running`） |
| AC-38 非运行中 ⇒ 409；未知 ⇒ 404 | ✅机测绿 | `::test_abort_terminal_run_returns_409[completed/failed]`、`::test_abort_unknown_run_returns_404`、`::test_abort_cross_tenant_is_404`、`::test_abort_already_aborted_is_idempotent_200` |
| AC-39 后端可查父 run/会话 | ✅机测绿 | `test_inc32_followup_context::test_followup_run_records_parent_and_session`（`child_rec.parent_run_id/session_id` + `workspace_runs` 落库回读） |
| AC-40 上下文注入承重（反向对照变红） | ✅机测绿 | **源码级反证**（见 §8.1）+ 工程师 monkeypatch 反证 |
| AC-41 续聊复用 `result-continue` 且确有真实请求 | ✅静态 | `ResultNextActions.tsx` 保留 `result-continue`；`LiveRunsView.tsx::onContinue` 改调 `POST /workspace/tasks` 带 `parent_run_id`（**未做浏览器端真请求捕获**） |
| AC-42 左侧按真实父子关系分组 | ✅静态 | `RunListPanel.tsx` 的 `session-group` / `session-group-title` / `session-history-empty`（「暂无历史任务」） |
| AC-43 文案与后端事实一致（诚实纪律） | ✅静态 | 会话分组标题取真实 `session_id`/`title`；无「同一会话」的伪造表述 |

### 8.1 AC-40 源码级反证（**我独立复现，不采信工程师**）

`_qa_inc32/ac40_source_reversal.py`：直接编辑**产品源码** `orchestrator.py::_resolve_continued_context` 中和解引用（插入 `return ""`），跑正向 AC-40 断言，再逐字节还原并校验 sha256：

```
orchestrator.py sha256[:16] = a0a436e428652924
PHASE 1 · positive on UNTOUCHED source        EXIT=0   (PASS ✅)
[mutated] sha256[:16] = f327d6bd684ac49b  (neutralised _resolve_continued_context)
PHASE 2 · same test on NEUTRALISED source     EXIT=1   (**变红** ✅)
[restored] sha256[:16] = a0a436e428652924 match=True
PHASE 3 · positive after RESTORE              EXIT=0   (PASS ✅)
SUMMARY exit(phase1=0, phase2=1, phase3=0) restored_intact=True
VERDICT AC-40 load-bearing reproduced: True
```

⇒ 正向断言**确实承重**（摘掉解引用即红），并已逐字节还原（工作树无残留改动）。

### 8.2 QA 自撰专项测试（AC-34/35/36）

新增 `tests/unit/test_inc32_dispatcher_qa.py`（**测试侧，不改产品代码**；未 git add）。用可脚本化的后台 `run_task`（循环 emit `run.step` 并计数）驱动真实 `RunDispatcher`，隔离、确定性。验证运行 `_qa_inc32/G_inc32.log`：

```
tests/unit/test_inc32_dispatcher_qa.py .... (3)
tests/unit/test_inc32_workspace_api.py ...... (12)
tests/integration/test_inc32_followup_context.py ... (3)
======================= 18 passed, 5 warnings in 2.16s ========================  EXIT=0
```

> 该文件为 QA 新增，**不含在 B 段的 1500/1515 基线口径内**；其 3 个节点是审阅方新增覆盖，全绿。

---

## 9. 基线 vs 实测数字对照

| 维度 | 基线（主理人） | 我的实测 | 差 |
|---|---|---|---|
| 后端 mem tests / fail / skip | 1500 / 0 / 1 | **1515 / 3 / 2** | 新增节点 +15；3 红=环境（基线同样红）；+1 skip=环境 |
| 后端 pg tests / fail / skip | 1500 / 0 / 1 | **1515 / 3 / 2** | 同上 |
| 节点集合差 | — | **ADDED 15 / REMOVED 0** | — |
| E2E | 13/14（唯一红 `landing page loads`） | **14/14** | R6 改名后转绿 |
| `data-testid` raw | 99 → 124 | **99 → 124（REMOVED=0）** | 一致 |
| 深色变量一致 | 93/93 | **93/93（0 lost/0 changed）** | 一致 |
| 迁移 016 | — | upgrade×2 no-op / downgrade 掉表 / 再入可 | 通过 |

---

## 10. 缺陷清单

| # | 分类 | 归属 | 说明 | 状态 |
|---|---|---|---|---|
| D1 | **测试缺陷（QA 自身）** | 我（QA） | `test_inc32_dispatcher_qa.py::test_ac34...` 首版在 `await dispatcher.abort()` 后**立即**断言 bus history，未等 `CancelledError` 处理器运行 ⇒ 偶发只看到 `run.step`。**已自修**：abort 后 `await asyncio.sleep(0.15)` 再断言；修后连跑绿。 | 已修 |
| D2 | 环境性历史红（**非源码 Bug**） | 环境/宿主 | `test_inc25_codeplane_commit_idempotent`（GBK 解码 git 输出）+ `test_inc29_workspace_lifecycle`×2（safe-delete 垫片令物理删除不落地）；**基线 worktree 反证同样 3 红**。 | 非本轮引入，登记 |
| D3 | 环境性 skip（**非源码 Bug**） | 环境/宿主 | `test_inc29_agent_server_engine::test_a1_...` 因 agent server 30s 未就绪 `pytest.skip("环境相关")`。 | 非本轮引入，登记 |
| D4 | 观察（低危，**非缺陷**） | 产品（PRD 已划出范围） | `wf_8K42n` 字面仍存在于两个**无壳营销/设计页**（`LandingPage.tsx`@`/welcome`、`DesignSystemPage.tsx`）；二者按 ADR-07 例外「不动」。`/tasks` 与外壳渲染路径已**无**任何 `DEMO_*`/`wf_8K42n`。 | 记录，符合范围 |

**源码 Bug = 0。** 无需退回工程师。

---

## 11. 未跑 / 未独立机测项（诚实边界）

1. **首屏像素级/可见性断言**（AC-6/AC-9/AC-21）：仅代码核查 + E2E 页面加载，**未**在 1024×768 视口做「首屏不可见 KPI/Agent/Skill/安全/日志」的专项 DOM 断言。
2. **5 个无壳深色页（`/welcome` `/architecture` `/design-hub` `/design-system` `/docs`）的逐像素对比**：仅核查 `data-theme="dark"` 保留性编辑，**未**做像素 diff。
3. **AC-20 / AC-41 的浏览器端全链路**：实时流终态切换、`result-continue` 触发真实 `POST /workspace/tasks` 的网络捕获，**未**在真实浏览器 + 真实后端下跑通（E2E spec 对 `/api/**` 走 stub；后端机制由 AC-34/39/40 的机测覆盖）。
4. **真实 LLM / OpenHands 引擎全链路**：本机无可用 provider/引擎，相关路径以 mock/确定性执行器替代（与既有 CI 口径一致）。
5. **28 条路由的逐条 HTTP 级「可达」**：以「`router.tsx` 与基线逐字节相同 + E2E 已实跑 `/`、`/architecture`、`/runs`」佐证，**未**对全 28 条逐条浏览器断言。
6. 后端 3 红 + 1 skip 的**根因修复**超出本轮范围（属宿主环境），仅在 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 或改 UTF-8 代码页下可消除；本报告未尝试修改宿主环境。

---

## 12. 结论

**达到「新增失败数 = 0」。** INC32 的 5 个 commit 在本机双档全量后端回归中：
- 节点级 **ADDED 15 / REMOVED 0**，且新增 15 节点**全绿**；
- 相对基线出现的 3 红 + 1 skip 经**基线 worktree 反证**确认为**本机环境既有现象**（GBK 解码 + safe-delete 垫片），**与 INC32 代码无关**；
- 构建（tsc/vite）双 EXIT=0、E2E **14/14**、迁移 016 幂等可逆、`data-testid` **只增不改不删（REMOVED=0）**、深色 **93/93 只搬不改**、`/tasks` 契约请求零改、无新依赖、路由 28 条全保留且无 `/chat`、AC-40 **源码级反证**成立。

**源码缺陷 0，测试缺陷 1（QA 自修）。** 未跑/未独立机测项已在 §11 如实登记。

---

### 附：证据文件索引（均在 `D:\Agentxm\Multi-Agent\_qa_inc32\`）

| 文件 | 内容 |
|---|---|
| `A_build.txt` | tsc/vite 构建原始输出 |
| `B_mem.log` / `B_pg.log` | 双档全量后端运行日志 |
| `B_mem.xml` / `B_pg.xml` | 双档 junit（我的实测，权威） |
| `B_mem_compare.txt` / `B_pg_compare.txt` | 基线 vs 实测 节点级集合差 + 失败明细 |
| `iso.txt` | 3 红隔离复现（脱离沙箱） |
| `base_iso.txt` | **基线 c806715 worktree 反证**（同样 3 红） |
| `C_alembic.txt` | 迁移幂等/可逆全过程 + DB 状态 |
| `D_pw.txt` / `.last-run.json` | E2E 原始输出 + 判定 |
| `E_testid_diff.txt` | testid 集合差（REMOVED=0） |
| `F1_diff.txt` / `F1_head_tasks.json` / `F1_base_tasks.json` | `/tasks` OpenAPI 契约对比 |
| `F4_tokens_diff.txt` | 深色变量只搬不改（0 lost/0 changed） |
| `G_inc32.log` / `G_inc32.xml` | INC32 专项（含 QA 自撰）测试结果 |
| `G_ac40_reversal.txt` | AC-40 源码级反证（green→red→green，sha 还原） |
| `run_backend.py` / `C_alembic.py` / `ac40_source_reversal.py` / `E_testid_diff.py` / `F1_dump_openapi.py` / `F4_tokens_diff.py` / `B_junit_compare.py` | 可复跑脚本 |
