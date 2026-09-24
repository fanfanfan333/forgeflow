# 09 · QA 独立复验报告（INC3）

> 复验人：QA（Edward）· 独立复验，**不引用任何工程师自报数字**，全部数字由 QA 自跑产出。
> 生成时间：2026-09-23（UTC）。
> 代码根：`D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main`
> 真 PG：`postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow`（容器已在 5433）

## 0. 方法与免责

- 本报告每项写：**命令 / 实际输出关键行 / 判定**。
- 「PASS」= QA 自己跑出的证据支持；「FAIL」= 证据不支持；「无法判定」= 环境/取证方式限制，如实标注。
- 取证方式有局限的（如本机无 `.git`），在对应条目显式说明。
- 单列两节：**未经我复核的项** 与 **我判定为假通过 / 口径不符的项**。

### 环境事实（QA 实测）

| 项 | 值 |
|---|---|
| venv python | `C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe`（Python 3.13.14） |
| node | `...\node\versions\22.22.2-3\node.exe` |
| 后端 | `http://127.0.0.1:8010`（QA 自起，`qa_tmp/run_api.py`） |
| 前端 | `http://127.0.0.1:5173`（QA 自起，vite dev + `VITE_API_TARGET=http://127.0.0.1:8010`） |
| 本机 `.git` | **不存在**（`fatal: not a git repository`）→ 迁移「未改动历史」用文件 mtime + 源码内容取证 |
| Bash 工具 | 残缺（无 `ls/grep/tail/dirname`）；本报告命令多经 Git Bash + 重定向到文件 |

---

## Phase 1 · 真 PG 结构与往返复验

### 1.1 迁移链

命令：`python -m alembic current | heads | history` / `upgrade head`（`POSTGRES_SYNC_URL=…@127.0.0.1:5433/forgeflow`）

输出关键行：
```
### alembic current (before)   → 011 (head)
### alembic heads             → 011 (head)        （单头，无分支）
### alembic upgrade head       → EXIT=0，current 仍 011
### helm history（线性）：010 -> 011, 009 -> 010, 008 -> 009, …, 001 -> <base>
```
`alembic/versions` 实际只有 `001…011`，**无 012**。

判定：**PASS**（单头线性的迁移链，`upgrade head` 幂等成功）。

### 1.2 011 是否未改动 010 及更早

- 文件 mtime（`Get-ChildItem … | Select LastWriteTime`）：
  - `001–009`：LastWriteTime = `2026/9/23 8:38:02`
  - `010_agentflow_hubs.py`：`2026/9/23 9:38:04`
  - `011_cost_slo_evolution.py`：`2026/9/23 14:53:45`
  → 011 的生成时间比 010 晚 **5h15m**，物理上不可能改动 010。
- 源码内容核对：`010` 只建 `agentflow_hubs` 12 表 + 6 列（`workspaces/workflow_runs/memory_vectors` 扩展）；`experiences` 的 `conflict_with / merged_from / dedup_key / confidence` **只出现在 011**。

判定：**PASS**（取证方式：mtime + 源码；本机无 git，无法用 `git diff`）。

### 1.3 011 升降级对称性

命令：`python -m alembic upgrade 010:011 --sql` / `downgrade 011:010 --sql`

- upgrade 产出：建 `cost_budgets / skill_ratings / context_build_stats` 3 表；`ALTER … ADD COLUMN IF NOT EXISTS` 共 **11 列**（skill_listings×4、experiences×4、skills×2、agent_approvals×1）。
- downgrade 产出：**逐条镜像**——逆序 `DROP COLUMN IF EXISTS` 同 11 列，再 `DROP TABLE IF EXISTS` 同 3 表，最后 `UPDATE alembic_version SET version_num='010'`。

判定：**PASS**（对称、幂等，`IF EXISTS/IF NOT EXISTS` 全覆盖）。

### 1.4 B4 四列 真 PG round-trip（**必须 PASSED，不得被 skip**）

命令 A（QA 自写 psycopg 直连探针）：`python qa_tmp/phase1_pg_roundtrip.py`

输出关键行：
```
alembic_version: ["011"]
columns: confidence=double precision(nullable); dedup_key=text(nullable);
         conflict_with=ARRAY/_uuid NOT NULL default "'{}'::uuid[]"; merged_from 同
roundtrip: sent==got for 全部四列（conflict_with 两值、merged_from 一值、dedup_key、confidence=0.87）
=== VERDICT: PASSED ===
```

命令 B（项目 pytest 用例，真 PG 档）：
`python -m pytest tests/unit/test_dedup_repo.py -v -rs -p no:cacheprovider`（`STORAGE_BACKEND=postgres`，`POSTGRES_URL=…5433/forgeflow`）

输出关键行：
```
tests/unit/test_dedup_repo.py::test_postgres_round_trip_four_columns PASSED [100%]
============================== 4 passed in 0.31s ==============================
```
用例名：`test_postgres_round_trip_four_columns`；**PASSED**（非 SKIPPED，`0 skipped`）。

判定：**PASS**（真 INSERT→SELECT 按值回读一致；用例是 PASSED 不是被 skip 的假绿）。

### 1.5 外部物证（第三方证据，独立于被测代码）

见 1.4 输出的 `information_schema.columns`（4 列的类型/可空/默认值）与 `alembic_version='011'`。均来自直连 5433 的独立查询，不经过任何工程师测试代码。

判定：**PASS**。

### 1.6 审计导出 `/audit/export`

命令：`python qa_tmp/phase1_audit_probe.py`（真 HTTP，Starlette TestClient 非 `with`；无 pool → 审计走进程内 ring buffer）

输出关键行 / 判定：

| 断言 | 关键行 | 判定 |
|---|---|---|
| json 格式 200 | `export_json_200` 200；`export_json_total_equals_store_at_call` export.total=15 == store_before=15 | PASS |
| json items 长度=total | `len=15 total=15` | PASS |
| search 与 store 一致 | `search.total=16 == store_before=16` | PASS |
| csv 200 + Content-Type | `text/csv; charset=utf-8`；CD 附件名 `audit-export-….csv` | PASS |
| csv 行数 = store | `data_rows=17 == store_before=17` | PASS |
| 非法格式 400 | `format=xml` → 400 | PASS |
| 时间窗 `to`/`from` | `to=2000` → total=0；`from=2999` → total=0 | PASS |
| 租户隔离 | export(无 workspace) 与 export(workspace W) 的 id 交集=0；W 侧非空(=1) | PASS |

**发现（诚实报告）**：`/audit/search` 与 `/audit/export` 的 total **不能**在一次会话里相等——因为**每个请求自身也被审计写一行**（观察者效应）。所谓「与 /audit/search 行数一致」只有在「同一时刻读同一存储」时才成立。QA 因此改用「各自 == 调用前 store 快照长度」证明两者读同一数据源；naive 的跨调用比较会恒差 1（记录于 `partB_observer_effect`）。

### 1.7 审计中间件顺序 · 拒绝留痕（安全项）

命令：同 `phase1_audit_probe.py`（真实 HTTP 请求，非只读代码）

输出关键行：
```
partA_status: no_token GET /audit/search → 401 ; viewer POST /experiences → 403
partA_login_codes: [503×10, 429, 429]   （anon 桶 10/min，第 11 次触发限流）
checks: 401_row_exists=true  / 401_outcome_denied=true  (outcomes=['denied'])
        403_row_exists=true  / 403_outcome_denied=true  (outcomes=['denied'])
        429_row_exists=true  / 429_outcome_label_is_denied=FALSE (outcomes=['error','error'])
```

判定：
- **拒绝留痕成立**：401 / 403 / 429 **三类都在审计里留下了一行**（`留痕` 这条要求 PASS）。
- **outcome 语义**：`middleware/audit.py:87-89` 只把 401/403 记为 `denied`，429 归 `error`（源码注释明确「429 rate-limit … downstream error」）。故 429 的 outcome **不是** `denied`。这属于**任务书期望与实现的口径不一致**，QA 未放宽断言掩盖，原样记录。

---

## Phase 2 · 固化可重复执行的 E2E 回归脚本

交付物：`frontend/scripts/e2e_verify.py`（Playwright + 系统 Chrome，channel=chrome）+ 结果 `frontend/scripts/e2e_verify_result.json`。

能力：启动前置检查（后端 `/health` + 前端 `/`）· 登录取 JWT 注入 sessionStorage · KPI 四卡文本断言 · Hero 标题与 4 装饰方块 `getBoundingClientRect` 数值不相交断言 · console error / pageerror 计数 · 四档视口 1440/1280/1024/768 · 退出码（全绿 0，任一断言失败非 0）· stdout + JSON 双输出。

命令：`python frontend/scripts/e2e_verify.py --out frontend/scripts/e2e_verify_result.json`

输出关键行：
```
backend : …8010 reachable=True (200)
frontend: …5173 reachable=True (200)
auth    : role=admin token=yes
[1440] status=200 kpi=['成功率','平均响应时间','总任务数','节省成本'] cubes_visible=4/4 overlaps=0 console_err=0 page_err=0
[1280] status=200 … cubes_visible=4/4 overlaps=0 console_err=0 page_err=0
[1024] status=200 … cubes_visible=0/4 overlaps=0 console_err=0 page_err=0   (VACUOUS)
[768]  status=200 … cubes_visible=0/4 overlaps=0 console_err=0 page_err=0   (VACUOUS)
VERDICT: PASS   EXIT=0
```

矩形数值表（1440/1280 为真实可见，数值证明不相交；1024/768 装饰块被 CSS 隐藏，不相交为空真）：

| 视口 | Hero 标题 rect (x,y,w,h) | 方块 c1..c4 x 区间 | 水平间隙 | 相交 | 备注 |
|---|---|---|---|---|---|
| 1440 | 297, 101, 512, 75 | 837 / 917 / 837 / 917（各宽 66） | 809↔837 = **28px** | 无 | 4 块可见 |
| 1280 | 297, 101, 352, 112.5 | 677 / 757 / 677 / 757（各宽 66） | 649↔677 = **28px** | 无 | 4 块可见 |
| 1024 | 297, 101, 620, 75 | 0×0（display:none） | — | 无 | **空真**（`nonintersectionVacuous=true`） |
| 768 | 297, 101, 386, 112.5 | 0×0（display:none） | — | 无 | **空真** |

判定：**PASS**（四档 KPI 文本齐全；1440/1280 数值不相交；1024/768 按设计隐藏并显式标注为「空真」；四档 console error=0、pageerror=0；退出码 0）。

> 说明：`@media (max-width:1180px){ .hero-art{display:none} }` —— 1024/768 档装饰方块本就隐藏，QA **未**把这条当作「不相交通过」的真实证据。

---

## Phase 3 · 浏览器验证出图（已完成）

命令（单次编排：起服务 → 等就绪 → 出图 → 停服）：
```bash
cd <repo> && python qa_tmp/run_api.py &                      # 后端 8010
cd frontend && VITE_API_TARGET=http://127.0.0.1:8010 node node_modules/vite/bin/vite.js --port 5173 --host 127.0.0.1 --strictPort &
python frontend/scripts/e2e_verify.py \
  --viewports 1440,1280,1200,1181,1180,1024,768 \
  --screenshot-dir docs/sop/shots --shots-paths /ops,/cost,/analytics \
  --out frontend/scripts/e2e_verify_result.json
```
结果：`VERDICT: PASS`，`E2E_RC=0`。

### 3.1 视口 7 档 · Hero 标题 vs 4 方块（数值，非目测）

| 视口 | 标题 rect (x,y,w,h) | 4 方块 x（各 w=66） | 标题右边界 | 最近方块左边界 | 水平间隙 | 相交 | cubes 可见 | 备注 |
|---|---|---|---|---|---|---|---|---|
| 1440 | 297, 101, 512, 75 | 837/917/837/917 | 809 | 837 | **+28px** | 无 | 4/4 | 真实可见 |
| 1280 | 297, 101, 352, 112.5 | 677/757/677/757 | 649 | 677 | **+28px** | 无 | 4/4 | 真实可见 |
| 1200 | 297, 101, 272, 150 | 597/677/597/677 | 569 | 597 | **+28px** | 无 | 4/4 | 真实可见 |
| 1181 | 297, 101, 253, 150 | 578/658/578/658 | 550 | 578 | **+28px** | 无 | 4/4 | **断点外侧**（>1180）仍可见 |
| 1180 | 297, 101, 620, 75 | 0×0 ×4 | 917 | — | — | 无 | 0/4 | **断点上**：`display:none` |
| 1024 | 297, 101, 620, 75 | 0×0 ×4 | 917 | — | — | 无 | 0/4 | `nonintersectionVacuous=true` |
| 768 | 297, 101, 386, 112.5 | 0×0 ×4 | 683 | — | — | 无 | 0/4 | `nonintersectionVacuous=true` |

- 7 档全部 `console_err=0`、`page_err=0`，KPI 四标签齐全。
- 1440/1280/1200/1181：数值证明标题与方块**不相交**，水平间隙恒为 **28px**。
- 1180/1024/768：装饰块被 CSS 隐藏（rect=0×0），不相交为**空真**，脚本已标注 `nonintersectionVacuous=true`（**不**计为真实几何通过）。
- **1181 与 1180 是断点两侧的实测边界**：1181 → 4/4 可见；1180 → 0/4 隐藏。与 `@media (max-width:1180px)` 语义一致。

### 3.2 产出文件（`ForgeFlow-main/docs/sop/shots/`，QA 本轮写入 10 张）

```
home_hero_fixed.png   251551 bytes   ← 主交付物（1440）
home_1280.png / home_1200.png / home_1181.png / home_1180.png / home_1024.png / home_768.png
ops_1440.png / cost_1440.png / analytics_1440.png
```
（目录内另有 5 张 `14:24` 旧图 home/home_scrolled/knowledge/security/skills，非本轮。）

已人工开图确认 `home_hero_fixed.png` 为真实渲染：左侧 Hero 标题、右侧 4 个渐变色方块（Agent/Skills/Memory/Security），无遮挡；KPI 行 = 成功率「—」/ 平均响应时间「—」/ 总任务数「0」/ 节省成本「—」。

### 3.3 `/ops`、`/cost`、`/analytics`（1440）

| 路由 | 状态 | console err | page err | 页面标题 | 关键 API（QA 抓包） | 判定 |
|---|---|---|---|---|---|---|
| `/ops` | 200 | 0 | 0 | 运维监控 · SLO | `/metrics/slo` 200 `source=metrics_source`；`/approvals` 200 total=26 | **空态**：三层 SLO 全「无数据」（开图确认为正确的空态页，非崩溃） |
| `/cost` | 200 | 0 | 0 | 成本与预算 | `/cost/board` 200 `has_data=true level=ok`；`/cost/savings` 200 `has_data=false amount=null` | **混合**：board 真实数据；savings 无基线 → 「—」（`amount` 为 `null` 而非 0，符合契约） |
| `/analytics` | 200 | 0 | 0 | 成本与预算 | 同 `/cost`（CostView 同一组件） | **混合**，同上 |

> 说明：脚本的 `dataState` 是粗判据（`/ops`=empty、`/cost`=real、`/analytics`=real）。**精确判定以上表为准**：`/cost`、`/analytics` 是「board 真实 + savings 空态」的混合态，不是全真实。
> 抓包里出现的 `/hooks.ts`、`/client.ts`、`/sse.ts` 是 dev server 的 `/src/api/*.ts` 模块（URL 含 `/api/` 被误匹配），**非后端 API 调用**；真实后端调用为 `/metrics/slo`、`/cost/board`、`/cost/savings`、`/approvals`。

### 3.4 ≤1180px 断点的其它响应式证据（只给证据，不下结论）

grep 全 `frontend/src` 命中 `1180` 的**仅** `frontend/src/styles/home.css`，共两处：

- `frontend/src/styles/home.css:75`
  ```css
  @media (max-width: 1180px) {
    .home { grid-template-columns: 1fr; }
  }
  ```
  受此影响：`.home` 的列布局由 `grid-template-columns: minmax(0, 1fr) 380px`（home.css:70）变为单列 `1fr` → 右侧栏 `.home-rail` 由「右侧固定列」变为「堆叠到主列下方」。
- `frontend/src/styles/home.css:207`
  ```css
  @media (max-width: 1180px) { .hero-art { display: none; } }
  ```

两条规则的**原文注释**（home.css 内）：

- home.css:93-95（`.hero` 定义处）：
  > `/* Two real columns: the copy takes the flexible one, the decorative art the fixed one, so the cubes can never sit on top of the title at ANY width. (Previously the art was absolutely positioned over the copy.) */`
- home.css:174-176（`.hero-art` 定义处）：
  > `/* Decorative 4 gradient cubes (Agent / Skills / Memory / Security). Static grid column (not absolute) so it is laid out beside the hero copy, never over it. Hidden once the layout stacks (see breakpoint below). */`

补充：同一份 `home.css` 另有 `@media (max-width: 900px)`（`.kpi-row` 4→2 列 @ home.css:211、`.agent-grid` @ :235、`.skill-row` @ :253），与 1180 断点无关。

### 3.5 服务状态

出图任务结束时由脚本主动 `kill` 了两个服务；随后 QA 已**重新起** 8010（API）与 5173（vite dev）。
**重要约束**：本机后台进程会被 harness 在约 2m43s 后回收（实测 `Status: killed, Duration: 2m 43s`），因此服务**不是长效**的。team-lead 若要查询，请在时限内，或让 QA 再次拉起。重启命令见 §附录。

> 取证坑：本机原生 `python.exe`/`node.exe` **不接受**绝对 POSIX 路径作为参数（`/d/...` 被解析成 `D:\d\...`）→ 一切命令以 `cd <repo>` + 相对路径执行。

## Phase 4 · 双档全量复跑 + Cost 链路独立验证 —— **已执行（QA 独立复跑，2026-09-23 晚）**

> 执行人：QA（Edward/严过关）。全部数字为本轮亲跑产出，证据文件均在 `qa_tmp/rc_*`。
> 快照纪律：跑前 `rc_snapshot_freeze.json`（387 文件）→ 全部验证后 `rc_snapshot_final.json`，
> `--compare` 结果 **SNAPSHOT_CLEAN**（added/removed/changed 全 0），证明验证期间无人改动
> `forgeflow/**`、`tests/**`、`frontend/src/**`、`alembic/**`——本页所有数字针对同一棵冻结树。
> 环境事实修正：工作区根 **存在** `.git`（`git rev-parse` = true，跟踪 ForgeFlow-main）；
> `ForgeFlow-main/` 自身无 `.git`。mtime+sha256 快照对「运行期间的未提交漂移」仍然有效且更严格。

### 4.1 双档全量 junitxml（0 fail / 0 error / 0 skip）

命令（两档均）：`python qa_tmp/rc_run_suite.py <memory|postgres>`
（= `python -m pytest tests -p no:cacheprovider --tb=line -q --junitxml=qa_tmp/rc_junit_<profile>.xml`，
memory 档 `STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock`；
postgres 档 `STORAGE_BACKEND=postgres` + `POSTGRES_*URL=…@localhost:5433/forgeflow`）

| 档 | pytest 摘要行 | junitxml 独立解析 | 判定 |
|---|---|---|---|
| memory | `716 passed, 95 warnings in 17.61s`，rc=0 | tests=716 failures=0 errors=0 **skipped=0**（`rc_junit_memory.xml`） | **PASS** |
| postgres | 全部 716 个用例在 stdout 全绿通过；进程 rc=1（**非测试失败**，见下注） | tests=716 failures=0 errors=0 **skipped=0**，testcases=716（`rc_junit_postgres.xml`） | **PASS** |

> 注（PG 档 rc=1 根因）：最后一个用例通过后、junitxml 已完整写出，会话收尾时沙箱
> safe-delete 批量守卫拦截了 pytest 临时目录清理（stderr 留有
> `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":64,"threshold":50,…}`）→ 退出码被拖成 1。
> 这是**环境/harness 干扰**，非任何用例失败。补强证据：真 PG 档子集
> `pytest tests/unit/test_dedup_repo.py tests/integration -q -rs` →
> `46 passed, 10 warnings in 6.12s`，**rc=0、0 skipped**（`qa_tmp/rc_pytest_pg_subset.txt`），
> 其中含 B4 四列真 PG round-trip 用例 `test_postgres_round_trip_four_columns`（PASSED 非 SKIPPED）。

### 4.2 真 PG 迁移链（复验项②前置）

命令：`alembic current / heads / upgrade head`（`POSTGRES_SYNC_URL=…@127.0.0.1:5433/forgeflow`）

输出（`qa_tmp/rc_alembic.txt`）：before=`011 (head)`；heads=`011 (head)` 单头；
`upgrade head` EXIT=0 幂等；after 仍 `011 (head)`。5433 端口连通性、docker 容器在跑（socket 探测 OPEN）。

判定：**PASS**。

### 4.3 真 Ollama 端到端闭环（复验项③）

命令：`python qa_tmp/rc_ollama_e2e.py`（`qa_tmp/rc_ollama_e2e.txt`）

形态：**真服务**——uvicorn 在本进程线程中监听 `http://127.0.0.1:8010`（真 lifespan、真 HTTP，
规避 harness ~2m43s 后台回收）；**真模型**——Ollama `qwen2.5vl:3b`（NO_PROXY 绕行沙箱代理）。
链路：User→FastAPI(8010)→/auth/login→POST /tasks→orchestrator→LangGraph/Agent→Ollama
→Tool→Validation→Experience→Memory。

关键输出：
```
/api/tags 200 models=['qwen2.5vl:3b','qwen3:8b']
POST /tasks 200 → run 8.2s 终态 completed
mode=llm  tokens=659  providers=['chat-ollama','chat-ollama']  degraded=None
plan(来自 LLM) = [research.search, data.query, analysis.score, report.render]
deterministic  = [research.search, data.query, code.run]        ← 不相等
experience_id=8243cd0f-… → GET /experiences 200，该 id 在库（items=1）
服务端日志：LLMPlanner[plan] OK via qwen2.5vl:3b in/out=355/94；[reflect] 173/37（真计费）
```
8/8 断言全过（在线 / completed / mode=llm / 非 mock provider / tokens>0 / 计划≠剧本 /
Experience 入库 / 无降级标记）。判定：**PASS**。

### 4.4 Durable Recovery（复验项④）

命令：`python qa_tmp/rc_durable_run.py`（驱动 `_durable_phase.py` 三相，**三个独立 OS 进程**；
证据 `qa_tmp/rc_durable_run.txt` + `rc_durable_{write,read,resume}.txt`）

真 PG + `AsyncPostgresSaver`（三相日志均确认 `graph.checkpointer = AsyncPostgresSaver`）。

```
thread = rc-durable-5323f2bd01e9
WRITE (进程1, RECURSION_LIMIT=3): GraphRecursionError 半途崩溃
        → 现场：stage='analyze', pending next=('analyzer',), tokens=88
READ  (进程2, 全新进程+全新连接): 读回 16 键完整状态，5 条 checkpoint 历史
        ([0] next=('analyzer',) stage='analyze' … [4] next=('__start__'))，tokens=88
RESUME(进程3, 全新进程): aupdate_state + ainvoke(None) 续跑
        → 'done'，tokens 88→208（只增剩余 +120），pending next=()
```
续跑非重跑：token 只从 88 增到 208（若从头重跑，qualify/research 的 88 会先被重复计入）；
崩在半途的 pending analyzer 被真正续上。与上轮基线（77→195）形态一致。判定：**PASS**。

> 过程记录（诚实披露）：首跑 `RECURSION_LIMIT=5` 时 3B 小模型 5 步已近终点，
> 崩溃落在 'done' 之后、resume 空转（tokens 198 不变）——跨进程持久化已证但「半途续跑」未证；
> QA 调整探针参数（5→3，**测试脚手架调整，非生产代码**）后复跑得到上方完整弧线。

### 4.5 Cost 链路三情形契约

命令：`python qa_tmp/rc_probe_cost.py`（TestClient 走完整中间件栈，memory 档；`qa_tmp/rc_cost_probe.txt`）

| 情形 | 关键实测 | 契约判定 |
|---|---|---|
| A 无数据 | `/cost/savings` `has_data=false, amount=null, baseline=null`；`/cost/board` `has_data=false, budgets=[]`；`/metrics/` `has_data=false, has_cost=false` | **PASS**（`amount` 是 `null` 不是 `0`） |
| B mock | 真 POST /tasks 跑通（completed, mode=deterministic, cost=0.0）→ `has_data=true` 但 `has_cost=false`；savings 仍 `has_data=false, amount=null` | **PASS**（mock 有运行无账单，不虚构成本） |
| C 付费定价+伪 token | 真定价表 `calculate_cost("gpt-4o-mini",…)` 定价伪 token（prev=0.06/cur=0.0135）注入账本 → savings `has_data=true, amount=0.0465 == prev×mult−cur`（mult=1.0，独立重算一致）；board `total_spent=0.0135`；metrics `has_cost=true` | **PASS** |

合计 **21/21 断言通过**。判定：**PASS**。

### 4.6 `openapi()` 与 `ROUTE_PERMISSION_MAP` 差集

命令：`python qa_tmp/rc_probe_routes.py`（`qa_tmp/rc_route_probe.txt/.json`）

输出：`path-items=69，(method,path) pairs=76，OPEN=6，MAPPED=70，UNMAPPED=0`。
判定：**PASS**（UNMAPPED=0，与期望一致；旧报告口径里的「openapi=75 / MAP=33」是更早版本的路由面，本轮实测面如上）。

### 4.7 权限回归（admin 通过 / viewer 写 403）

命令：`python qa_tmp/rc_probe_perms.py`（`qa_tmp/rc_perm_probe.txt`）

```
admin  POST /marketplace/skills/publish    -> 404 (业务逻辑到达，非 403)
admin  POST /marketplace/skills/x/install  -> 404 (同上)
admin  POST /marketplace/templates/refresh -> 200 {"refreshed":true,"total":4}
viewer POST /marketplace/skills/publish    -> 403 (Role 'viewer' cannot write marketplace)
no-tok POST /marketplace/skills/publish    -> 401
```
判定：**PASS**。

### 4.8 前端构建门禁（消化「未复核」清单）

命令：`node node_modules/typescript/bin/tsc -b`；`node node_modules/vite/bin/vite.js build`
（`qa_tmp/rc_fe_tsc.txt` / `rc_fe_vite.txt`）

输出：`TSC_EXIT=0`、`VITE_EXIT=0`。判定：**PASS**。

### Phase 4 总结论

**全部 PASS**：双档 716/716（junitxml 0 fail/0 error/0 skip）· 迁移链单头 011 幂等 ·
真 Ollama 闭环 8/8 · Durable 跨进程恢复（88→208）· Cost 21/21 · UNMAPPED=0 · 权限回归 · 前端构建。
全程 SNAPSHOT_CLEAN。PG 档全量的 rc=1 为沙箱批量删除守卫在会话收尾的环境干扰（junitxml 与
子集 rc=0 双重佐证非测试失败），已如实标注，不计入源码问题。

---

## 未经我复核的项（NOT VERIFIED BY QA）

> 2026-09-23 晚 Phase 4 执行后逐条消化如下；仍遗留的仅「文档册」一项（非运行时断言，见末条）。

- ~~真 PG 全量档「24 failed → 0」的收敛结果~~ → **已复核（PASS）**：本轮亲跑 junitxml tests=716 / failures=0 / errors=0 / skipped=0（§4.1），PG 子集 rc=0（46 passed）。
- ~~memory 档 `624 passed / 0 failed`~~ → **已复核（PASS）**：本轮实测为 **716 passed / 0 failed**（旧数字 624 是更早版本套件规模，以本轮 716 为准；§4.1）。
- ~~路由探针 `openapi()=75` / `ROUTE_PERMISSION_MAP=33` / `UNMAPPED=0`~~ → **已复核（PASS）**：UNMAPPED=0 成立；路由面本轮实测 69 path-items / 76 pairs / 6 open / 70 mapped（旧 75/33 为更早版本口径；§4.6）。
- ~~前端构建 `tsc -b EXIT=0`、`vite build EXIT=0`~~ → **已复核（PASS）**：本轮亲跑 TSC_EXIT=0、VITE_EXIT=0（§4.8）。
- ~~Cost 通电链路端到端（WS-B 进行中）~~ → **已复核（PASS）**：三情形契约 21/21（含 /cost/savings 的 has_data/amount 语义与 KPI#3 数据源；§4.5）。
- 文档册（WS-C）→ **未复核**：属文档交付物而非可执行断言，本报告只覆盖运行时/契约类验证；文档完整性由 team-lead 在收口时核对。
- ~~Phase 4 全部子项~~ → **已全部执行**（§4.1–§4.8，全 PASS）。

> Phase 3 已于上一轮完成（见上）。

## 我判定为假通过 / 口径不符的项（FALSE-PASS / MISMATCH）

1. **429 拒绝的 outcome 标签**：任务书期望「429 也留 `outcome=denied`」，实测为 `outcome=error`（仅 401/403 → denied）。**留痕成立，标签口径不符**。非放宽断言所致，如实曝光。
2. **`/audit/export` 与 `/audit/search` 行数「一致」**：若不考虑「每个请求自审计」的观察者效应，naive 断言必然差 1 → 会误导为失败。QA 已改用「同源快照」证明两者读同一存储。
3. **1024/768 的 Hero 不相交**：因装饰块 `display:none`，不相交是**空真**。QA 显式标注，**不**计入真实几何通过。

---

## 附：可复现命令清单

```bash
# 后端（8010）
python qa_tmp/run_api.py                 # 见文件内 SelectorEventLoop 说明
# 前端（5173）
cd frontend && VITE_API_TARGET=http://127.0.0.1:8010 node node_modules/vite/bin/vite.js --port 5173 --host 127.0.0.1 --strictPort
# Phase 1
python -m alembic current && python -m alembic heads && python -m alembic history
python -m alembic upgrade 010:011 --sql ; python -m alembic downgrade 011:010 --sql
python qa_tmp/phase1_pg_roundtrip.py
python -m pytest tests/unit/test_dedup_repo.py -v -rs -p no:cacheprovider
python qa_tmp/phase1_audit_probe.py
# Phase 2
python frontend/scripts/e2e_verify.py --out frontend/scripts/e2e_verify_result.json
# Phase 3（6 档 + 边界 1180 + 三页出图）
python frontend/scripts/e2e_verify.py \
  --viewports 1440,1280,1200,1181,1180,1024,768 \
  --screenshot-dir docs/sop/shots --shots-paths /ops,/cost,/analytics \
  --out frontend/scripts/e2e_verify_result.json
# Phase 4（本轮 QA 独立复跑）
python qa_tmp/mtime_snapshot.py --label rc_freeze --out qa_tmp/rc_snapshot_freeze.json
python qa_tmp/rc_run_suite.py memory            # 离线档全量 + junitxml
python qa_tmp/rc_run_suite.py postgres          # 真 PG 档全量 + junitxml
python qa_tmp/rc_ollama_e2e.py                  # 真 8010 服务 + 真 qwen2.5vl:3b 闭环
python qa_tmp/rc_durable_run.py                 # 真 PG Durable：crash→read→resume 三进程
python qa_tmp/rc_probe_cost.py                  # Cost 三情形契约 21 断言
python qa_tmp/rc_probe_routes.py                # openapi vs ROUTE_PERMISSION_MAP 差集
python qa_tmp/rc_probe_perms.py                 # admin/viewer/no-token 权限回归
python qa_tmp/mtime_snapshot.py --compare qa_tmp/rc_snapshot_freeze.json qa_tmp/rc_snapshot_final.json
```

## 附：文件归属

- `ForgeFlow-main/qa_out.txt`（内容为 alembic `001→011` 链与迁移目录探针输出）——**是 QA（Edward）本轮产出的临时探针文件**，归档时可安全删除，非他人成果。
- QA 独占产出：`frontend/scripts/e2e_verify.py`、`frontend/scripts/e2e_verify_result.json`、`docs/sop/09-QA-REPORT-INC3.md`、`docs/sop/shots/*`、`qa_tmp/**`。

## 附：冻结与快照纪律（Phase 4 用；本轮已固化工具）

本项目**无 git**，无法用 commit 固化快照；且本轮多次出现「移动靶」（源码在被并发编辑时跑全量 → 数字不可复现）。因此 Phase 4 采用 **mtime+sha256 双快照**纪律：

```bash
# 收到冻结广播后，立刻记录基线
python qa_tmp/mtime_snapshot.py --label freeze --out qa_tmp/snapshot_freeze.json
#   ... 跑全量双档 + Cost + 门禁 + 权限回归 ...
# 跑完再记录一次
python qa_tmp/mtime_snapshot.py --label after  --out qa_tmp/snapshot_after.json
# 比对：clean ⇒ 期间无人改动；drift ⇒ 数字作废并立即上报
python qa_tmp/mtime_snapshot.py --compare qa_tmp/snapshot_freeze.json qa_tmp/snapshot_after.json
```

- 监视根：`forgeflow/**`、`tests/**`、`frontend/src/**`、`alembic/**`（跳过 `__pycache__`/`node_modules` 等）。
- 每文件记录 `mtime_ns + size + sha256`；两次一致 → `SNAPSHOT_CLEAN`（exit 0）；有增删改 → `SNAPSHOT_DRIFT_DETECTED`（exit 1）并列出 `added/removed/changed`。
- 自测：两次连续快照 `files=378`，`clean=true`，`COMPARE_RC=0`。

## 附：服务重启命令（后台进程会被 harness ~2m43s 回收，按需重拉）

```bash
cd <repo>
python qa_tmp/run_api.py > qa_tmp/api_run.log 2>&1 &          # 8010
cd frontend && VITE_API_TARGET=http://127.0.0.1:8010 node node_modules/vite/bin/vite.js --port 5173 --host 127.0.0.1 --strictPort > ../qa_tmp/vite_run.log 2>&1 &
```
