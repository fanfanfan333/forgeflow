# AgentFlow P0 改造 — 独立验收报告（QA / 严过关 Yan）

> 2026-09-26 INC16：OpenAI/Anthropic provider 已移除，本节为历史记录。

- 日期：2026-09-23
- 被测对象：`ForgeFlow-main`（Python `forgeflow/` + React 19/Vite `frontend/`）
- 运行环境：`STORAGE_BACKEND=memory`、`LLM_PROVIDER=mock`、`EMBEDDING_PROVIDER=mock`（本机无 PostgreSQL / 无 Ollama）
- 依据：`docs/sop/01-PRD.md`、`docs/sop/02-ARCHITECTURE.md`、`docs/sop/03-TASKS.md` §4 验收清单
- 立场：**独立证伪**，不复核工程师自报；每个结论均附命令/输出/代码位置证据。

---

## 0. 结论速览

| 验证项 | 结论 | 级别 |
|---|---|---|
| V1 回归门禁未被"作弊"通过 | ✅ 通过 | — |
| V2 测试基线复现（390/3/0） | ✅ 通过（数字一致） | — |
| V3 闭环端到端（HTTP 全链路） | ✅ 通过 | — |
| V4 fail-closed 路由门禁 | ✅ 通过（65 路由全登记） | — |
| V5 租户隔离与越权 | ✅ 通过（fail-closed） | — |
| V6 Policy 风险分级 + HITL | ✅ R2 通过（入口 sensitive 拦截 + 运行时逐 step 闸门） | — |
| V7 DLP 与工具输出防护 | ✅ R2 通过（cn_id 已检出并拦截） | — |
| V8 旧接口未被破坏 | ✅ R2 通过（`/memory/search` 200） | — |
| V9 前端（build / 硬编码 / 区块 / SSE） | ✅ R2 通过（完整 build 零报错 + 真轮询 + 红点） | — |
| V10 迁移与配置一致性 | ✅ 通过 | — |
| 额外：审计留痕丢失 | ✅ R2 通过（非 UUID workspace 不再丢行） | — |

**Round 1 判定：`Engineer`** —— 发现 3 个源码缺陷（1×P1 DLP 漏检、1×P1 旧接口 500、1×P1 审计丢失）+ 1 个额外 P1（审计丢行）。未发现测试代码自身缺陷。

**Round 2 判定：`NoOne`（全部通过）** —— 工程师已修复全部 P1 与 2 个 P2，QA 逐项独立复核确认（见 §14）。完整回归 `407 passed / 3 skipped / 0 failed`（EXIT=0），前端完整 `vite build` 零报错（`✓ built in 4.59s`, EXIT=0）。无新增缺陷，无残留阻断项。2 轮上限内收敛。

工程师自报 `390 passed / 3 skipped / 0 failed`、前端 build 零报错 —— **与实测一致，无 discrepancy**；但其 `IS_PASS: YES` 的判定**过早**：遗漏了 3 个未被其自测覆盖的缺陷（见 §11）。

---

## V1 回归门禁是否被"作弊"通过 — ✅ 通过

**方法**：非 git 仓库（`ForgeFlow-main\.git` 不存在），改用"文件计数 + 抽样审读"。

**证据**：
1. 文件计数（`Glob`）：
   - `tests/unit`：45 个 `.py`（44 个 `test_*.py`），基线 36 → **无删除**，新增约 8 个新功能测试（`test_experience_extraction.py`、`test_candidate_compiler.py`、`test_skill_versioning.py`、`test_skill_evaluator.py`、`test_governance_gate.py`、`test_policy_engine.py`、`test_tenant_isolation.py`、`test_memory_scopes.py`、`test_security.py`）。
   - `tests/integration`：9 个 `.py`（8 个 `test_*.py`），基线 7 → 新增 `test_runs_sse.py`，**无删除/重命名**。
2. mtime 分组：既有测试文件 mtime 集中在 `2026-09-23 08:38:05`（改造前基线），新功能测试与 `tests/conftest.py` 在 `10:00+`。
3. 抽样审读 3 个既有测试（全文阅读，断言强度未削弱）：
   - `tests/unit/test_graph.py`（13 tests，含真实节点/边断言）
   - `tests/unit/test_rbac.py`（9 tests）
   - `tests/unit/test_multi_tenant.py`（8 tests）
4. 关键嫌疑文件 `tests/conftest.py`（10:02:28 被改动）审读：改动**纯新增** `os.environ.setdefault("STORAGE_BACKEND"/"LLM_PROVIDER"/"EMBEDDING_PROVIDER"/"DEV_LOGIN_*"/"FORGEFLOW_ALLOW_TEMPLATE_WORKFLOWS", ...)`（conftest.py:19-30），使用 `setdefault` 不覆盖既有值；未改动 `_stub_dns`、`mock_llm`、`mock_pool` 等既有 fixture 语义。
5. 全 `tests/` 搜索 `pytest.skip|xfail|assert True|@pytest.mark.skipif`：既有文件**零命中**（新增的 `test_auth_db.py` 属既有 DB 测试，见 V2）。

**结论**：未发现"放宽/删除既有断言以掩盖失败"的行为。

---

## V2 亲自复现测试基线 — ✅ 通过

**命令**（venv + 离线变量）：
```
STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock \
  python -m pytest tests/ -q -p no:cacheprovider
```
**证据**（`.qa-tmp/qa_pytest_full.txt` / `qa_pytest_skips2.txt`）：
```
collected 393 items
... ================ 390 passed, 3 skipped, 42 warnings in 22.71s ================
EXITCODE=0
```
与工程师自报 **完全一致**。

**3 个 skipped 的真相**（`qa_pytest_skips2.txt`）：
```
tests\integration\test_auth_db.py sss
```
即 3 个 skip 全部来自 `tests/integration/test_auth_db.py`（`test_rotation_and_reuse_detection` / `test_unknown_token_rejected` / `test_happy_path_multi_rotation`）。原因见该文件 `pool` fixture（test_auth_db.py:33）：
```python
pytest.skip(f"live Postgres not reachable ({exc})")
```
本机无 PostgreSQL → 干净跳过。**这 3 个是既有的 refresh-token 轮换 DB 集成测试，与本次 AgentFlow 新功能无关，不掩盖任何新功能失败。** 属合理跳过。

---

## V3 闭环端到端（HTTP 全链路）— ✅ 通过

用 `fastapi.testclient` 真跑 HTTP（非单测）。关键请求/响应（`.qa-tmp/qa_verify_out.txt`）：

| 步骤 | 请求 | 响应关键字段 |
|---|---|---|
| 建任务① | `POST /tasks {intent:"分析销售数据并生成报告"}` | `200 {'run_id':'02acf484-…','status':'completed'} exp=1d6e85b4-…` |
| 建任务② | `POST /tasks` | `200 run_id=43b817be-… exp=5602bf4c-…` |
| 建任务③ | `POST /tasks` | `200 run_id=c0adb15d-… exp=4a4045b6-…` |
| 查经验 | `GET /experiences` | `200 total=3`（sample `{id:4a4045b6…, run_id:c0adb15d…, outcome:'success'}`） |
| 追溯血缘 | `GET /experiences/{id}/lineage` | `200 run present=True run_id=c0adb15d-…` |
| 编译候选 | `POST /skill-candidates` | `200 {'id':'e3d1a9ff-…','status':'draft','similarity_score':1.0}` |
| 四要素校验 | — | `draft_spec` prompt/steps/tools/io_schema **均非空 True**（steps/tools=`["research.search","data.query","code.run"]`） |
| 评估 | `POST /skill-candidates/{id}/evaluate` | `200 {'verdict':'pass','metrics':{…,'score':1.0}}` |
| 固化 | `POST /skill-candidates/{id}/promote` | `200 {'skill_id':'bb0a476c-…','semver':'0.1.0','approved_by':'admin'}` |
| 版本列表 | `GET /skills/{id}/versions` | `200 count=1` |
| 发新版 | `POST /skills/{id}/versions` | `200 0.2.0` |
| 回退 | `POST /skills/{id}/rollback` | `200 current_version=0.1.0` |

失败路径：`POST /tasks(fail) → 200 status=failed replans=2`；`GET /experiences?outcome=failure → 200 total=4`。
**闭环（任务→经验→候选→评估→固化→版本→回退）端到端成立，无 P0 缺陷。**

---

## V4 fail-closed 门禁（全量枚举，非抽样）— ✅ 通过

**方法**：从 FastAPI `app.openapi()` 枚举全部 (method, path) 与 `forgeflow/rbac/policies.py` 的 `ROUTE_PERMISSION_MAP` 做差集；再对每条非 OPEN 路由发无 token 请求。
**证据**（`.qa-tmp/qa_v4_out.txt`）：
```
TOTAL (method,path) in OpenAPI: 65
UNMAPPED (registered but not in ROUTE_PERMISSION_MAP): 0
NON-401 routes: []
```
- 65 条路由全部登记；新增 7 个 router 的路由（tasks/runs/experiences/skills/policies/security/approvals_hub）均在报表中标记 `mapped`。
- OPEN（豁免 RBAC）仅：`/health`、`/docs`、`/openapi.json`、`/redoc`、`/docs/oauth2-redirect`、`/auth/{login,introspect,refresh,logout,oidc/exchange}`。
- 无 token 请求 **全部 401**（含新增路由），无 fail-open。

---

## V5 租户隔离与越权（对抗性）— ✅ 通过

**证据**（`.qa-tmp/qa_verify_out.txt` §V5）：
```
A creates experience -> 200 id=c56506ce-…
B lists experiences  -> total=0  leaksA=False
B reads A's exp by id -> 404
A creates skill -> B sees A skill = False
viewer POST /skills            -> 403
viewer POST /policies          -> 403
viewer POST /skill-candidates  -> 403
B promotes A's candidate       -> 404
```
**边界用例**：
- `tenant_id` 缺失（无 workspace claim）→ 落到 `DEFAULT_TENANT_ID=default`，返回本租户 4 条（未跨租户）。
- 伪造 `tenant_id` 字符串 → `total=0`（无数据泄露，fail-closed）。
- `tenant_id=""` → 等价缺省 → 4 条（本租户内）。
跨租户读/写/promote 三种路径全部被拦截，无数据泄露。

---

## V6 Policy 风险分级 + HITL — ⚠️ 形式验收通过 + 架构风险（P2）

**证据**（`.qa-tmp/qa_verify_out.txt` §V6）：
```
evaluate(transfer:execute)  -> risk=high requires_approval=True approval_id=12fe4f48-…
evaluate(database:delete)   -> risk=high requires_approval=True approval_id=f803228e-…
evaluate(email:send)        -> risk=high requires_approval=True approval_id=67dc8f5e-…
evaluate(metrics:read)      -> risk=low  requires_approval=False
pending approvals after high-risk evals -> 4
reject approval -> 200 status=rejected
ABAC deny for blocked-user -> {'effect':'deny','hit':True}
```
高风险 100% 产出 `requires_approval=True` + 持久化 Approval 行；低风险自动放行 —— **满足 §4 P0-09 的验收判据**（`requires_approval=True` 且生成 `Approval`）。

**⚠️ P2 架构风险（工程师自测盲区）**：HITL 的"阻断执行"仅在 **API 入口**成立，且入口判据恒为低风险：
- `forgeflow/api/routers/tasks.py:31-40` 对 `POST /tasks` 调用 `evaluate_task_entry`，其内部固定评估 `resource="workflows", action="execute"`（`governance/policy_engine.py:187-198`）；按 `classify_risk`（policy_engine.py:31-49），`workflows:execute` 恒判 **low** → `requires_approval` 恒为 False → `/tasks` 永不阻断。
- 全量搜索 `forgeflow/runtime/**` 对 `PolicyEngine|requires_approval|evaluate_action` **零命中**：运行时的工具执行路径**未接入** PolicyEngine。
- 因此，"高风险动作阻断执行"仅作为独立 `/policies/evaluate` + skill-promote 闸门存在；**运行中任务的真实高风险工具调用不会被运行时拦截**。§4 验收未要求该端到端链路，故仅记为风险/设计待补齐，非 P0。
- "拒绝审批后动作确实没有执行"**无法端到端验证**（无独立执行器消费 Approval）；已实测的仅是审批状态流转 `pending→rejected`。

---

## V7 DLP 与工具输出防护 — ⚠️ 部分通过：**身份证 PII 漏检（P1）**

**通过项**（`.qa-tmp/qa_verify_out.txt` §V7）：
```
scan[email]       pii=True cats=['email']       redacted='…[REDACTED:email]…'
scan[ssn]         pii=True cats=['ssn']
scan[credit_card] pii=True cats=['credit_card']
scan[cn_mobile_11] pii=True cats=['phone']
scan_outbound(email to non-allowlisted) blocked=True reason=outbound PII blocked: email
sanitize_tool_output(injection) wrapped=True redacted=True
out: <UNTRUSTED_TOOL_OUTPUT name="web"> [REDACTED: tool output flagged as prompt-injection attempt. Reasons: instruction_override] </UNTRUSTED_TOOL_OUTPUT>
```
工具输出注入已用 `<UNTRUSTED_TOOL_OUTPUT>` 包裹并降级为 REDACTED。

**❌ 缺陷 P1：中国居民身份证号（18 位）不被识别**
```
scan[cn_id_18] pii=False cats=[] redacted='身份证 110101199003071234'   # 原文未脱敏
```
根因：`forgeflow/security/pii_redactor.py` 仅实现美国 SSN 正则 `_SSN_RE = r"\b\d{3}-\d{2}-\d{4}\b"`（第 28 行），**无中国身份证模式**；文档亦未列该类别（第 8-14 行）。
影响：外发内容若仅含身份证号，`scan_outbound` **不会拦截**；PRD（§7/需求）明确要求"身份证/手机号/邮箱"PII 防护 → 该类别漏检。
**期望**：`cn_id_18` 应 `pii=True` 且被 REDACTED / 外发被 block；**实际**：`pii=False`，原文放行。

---

## V8 旧接口未被破坏 — ⚠️ 部分通过：**`/memory/search` 500（P1）**

**证据**（`.qa-tmp/qa_verify_out.txt` §V8）：
```
GET /agents                -> 200 (307→/agents/)
GET /metrics/              -> 200
GET /marketplace/templates -> 200
GET /workspaces/           -> 200
GET /approvals/pending     -> 200
GET /memory/scopes         -> 200
GET /memory                -> 200
GET /memory/search?q=hello -> 500        ← 缺陷
POST /memory/store         -> 403 (namespace must start with 'global/' — 预期 fail-closed)
```
**❌ 缺陷 P1：`GET /memory/search` 在文档化的离线环境返回 500**
- 调用栈（同文件 §V8 上方 traceback）：
  `forgeflow/api/routers/memory.py:111 search_memory → MemoryManager.recall → pgvector_store.search → pgvector_store._get_embeddings → OpenAIEmbeddings(...) → openai.OpenAIError: Missing credentials`
- 根因：旧 `memory` 路由的**搜索**路径走 legacy `MemoryManager`/`pgvector_store`（PG + OpenAI Embeddings），**未接入**新 Repository 层，亦**未遵循** `STORAGE_BACKEND=memory` / `EMBEDDING_PROVIDER=mock`；而同文件的 `/memory`（列表）、`/memory/scopes` 已走新仓储层（正常 200），造成新旧不一致。
- §4 T12 验收明文要求"旧 `/memory/store`、`/memory/search` **不破**"，在 `STORAGE_BACKEND=memory LLM_PROVIDER=mock` 环境该断言**被违反**。
- 说明（诚实标注）：本机无 PG 基线历史（非 git 仓库），**无法确证该 500 是本次改动新引入还是既有的"memory 模式下搜索始终依赖 PG/OpenAI"**；但按验收口径"离线环境旧接口不破"判为缺陷。

> 备注：`GET /workflows`→404、`GET /audit`→404 不是缺陷——真实路由为 `/workflows/{run_id}`、`/audit/search`（见 V4 报表），本项仅为探测。

---

## V9 前端 — ✅ 通过（含 2 处 P2 说明）

**V9.1 build 零报错** ✅（`.qa-tmp/frontend_build2.txt`）：
```
vite v8.1.2 building client environment for production...
✓ 1947 modules transformed.
✓ built in 4.10s
EXIT=0
```
仅有一条 chunk >500kB 的**警告**（非错误）。

**V9.2 首页无硬编码假数据** ✅
- 全 `frontend/src` 搜索 `1,248 / 8,642 / 96.8 / 18.4 / 华东地区 / 任务规划师 / 客户流失分析 / 合同审查 / 项目复盘 / 代码质量检查`：**零命中**。
- 深查 `views/HomeView.tsx`：`SUGGESTIONS`（4 个建议词）、`AGENT_ICON`（emoji 映射）、`CUBES`（4 个装饰方块）**均为展示性常量**，非"接口数据兜底"；所有业务数据（KPI/Agent/技能/近期任务/日志/安全概览）均来自 `api/hooks.ts` 的真实 hooks，无本地常量数组冒充接口数据。
- KPI #3 用 `/metrics` 真实累计成本替代 PRD 中无接口支撑的"节省成本"（HomeView.tsx:13-14 注释，做法合理）。

**V9.3 首页 §7 区块逐项清点**：
| 区块 | 状态 | 证据 |
|---|---|---|
| 左侧栏：品牌区 + 10 项导航 + 底部渐变卡 | ✅ 实现 | `Sidebar.tsx:33-50`（9 项 NAV + 系统设置展开=10）、`:109-113`（底部文案） |
| 顶部栏：搜索框 / ⌘K / 铃铛 / 用户信息 | ⚠️ 部分 | `Topbar.tsx:61-70`（搜索+⌘K）、`:82`（铃铛）、`:93`（AuthControls=用户）；**铃铛无红点**（PRD 要求"红点"） |
| Hero：大标题/副标题/任务输入框/4 Chip/4 立体方块 | ✅ 实现 | `HomeView.tsx:102-144`（4 cubes）、`:132-138`（4 chips） |
| 4 个 KPI 卡 | ✅ 实现 | `HomeView.tsx:151-195` |
| 我的 Agent 2×3 六卡 | ✅ 实现 | `HomeView.tsx:235-264`（`agent-grid` 6 卡 + loading 6 骨架） |
| 技能中心 4 卡 | ✅ 实现 | `HomeView.tsx:286-315`（`useFeaturedSkills(4)`） |
| 右栏：近期任务 / 智能执行日志 / 安全与资源概览 | ✅ 实现 | `HomeView.tsx:74-78` 三卡 |

**V9.4 SSE** ✅ `GET /runs/{id}/events` 实测（`.qa-tmp/qa_sse_out.txt`）：
```
status 200  ct text/event-stream; charset=utf-8  x-accel no
has_data_frames: 9   has_DONE: True
frames: run.started → run.step → run.step.done ×3 → run.completed
tail: data: [DONE]
```
真推 `data:` 帧且 `[DONE]` 收尾；`run.completed` 携带 `experience_id`（闭环内生经验）。

**⚠️ P2 说明 1**："断流降级为轮询"**仅文案、未真正实现**：`hooks/useRunEvents.ts:53` 在 SSE 出错时仅 `setError`；`HomeView.tsx:418-422` 只渲染一行"SSE 不可用，已降级为轮询"，**并无对事件流的轮询兜底**（仅有 `useRecentHubRuns` 每 10s 轮询"任务列表"，不重建执行日志）。属误导性 UI 文案。
**⚠️ P2 说明 2**：`Topbar.tsx:44-49` 组织切换器硬编码"演示工作区 · Acme"、"prod-us-east-1"（装饰性，但形式上仍是硬编码数据，非取接口）。

---

## V10 迁移与配置一致性 — ✅ 通过

**V10.1 迁移 010** ✅：
- `alembic/versions/010_agentflow_hubs.py`：`revision="010"`、`down_revision="009"`；模块可 import 且**不触发 DB 连接**。
- 离线 SQL 对称性（无 PG，用 `alembic ... --sql`）：
  - `upgrade head --sql`：干净生成 `009 → 010`（`CREATE TABLE …`）。
  - `downgrade 010:009 --sql`：干净生成对称 `DROP TABLE …`。
  - 表数量一致（升级/降级对称）。

**V10.2 `config.py` ↔ `.env.example`** ✅ **10 ↔ 10 一一对应，默认值一致**：
| config.py 字段 | 默认 | .env.example | 值 |
|---|---|---|---|
| `storage_backend` | postgres | `STORAGE_BACKEND` | postgres |
| `embedding_provider` | openai | `EMBEDDING_PROVIDER` | openai |
| `embedding_dimension` | 1536 | `EMBEDDING_DIMENSION` | 1536 |
| `skill_candidate_min_experiences` | 3 | `SKILL_CANDIDATE_MIN_EXPERIENCES` | 3 |
| `skill_candidate_similarity_threshold` | 0.85 | `SKILL_CANDIDATE_SIMILARITY_THRESHOLD` | 0.85 |
| `skill_featured_limit` | 4 | `SKILL_FEATURED_LIMIT` | 4 |
| `tenant_isolation_level` | row | `TENANT_ISOLATION_LEVEL` | row |
| `default_tenant_id` | default | `DEFAULT_TENANT_ID` | default |
| `dlp_enabled` | true | `DLP_ENABLED` | true |
| `max_replan_attempts` | 2 | `MAX_REPLAN_ATTEMPTS` | 2 |

> 备注（既有、非本次新增字段）：`.env.example` 的 `POSTGRES_SYNC_URL` 为 `postgresql://`，而 `config.py` 默认值为 `postgresql+psycopg://`，二者不一致，建议顺带对齐。

---

## 11. 缺陷清单（分级 + 复现 + 期望/实际）

### P1-1 身份证 PII 漏检（DLP）
- **文件**：`forgeflow/security/pii_redactor.py:22-35`（缺 CN 身份证正则）
- **复现**：`DlpGate().scan("身份证 110101199003071234")`
- **期望**：`pii=True, cats=['cn_id']`，`scan_outbound` 拦截
- **实际**：`pii=False, cats=[]`，原文放行
- **修复建议**：新增中国居民身份证 18 位（含末位 X 校验位）正则并纳入 `patterns`；同步 `scan_outbound` 拦截类别与文档。

### P1-2 `GET /memory/search` 离线环境 500
- **文件**：`forgeflow/api/routers/memory.py:111`（search 走 legacy `MemoryManager`），`forgeflow/memory/pgvector_store.py:31`
- **复现**：`STORAGE_BACKEND=memory EMBEDDING_PROVIDER=mock` 下 `GET /memory/search?q=hello`
- **期望**：200（离线可用）或明确降级；**实际**：500 `openai.OpenAIError: Missing credentials`
- **修复建议**：search 路径改走新 Repository/embedding 抽象，或让 `pgvector_store` 尊重 `EMBEDDING_PROVIDER=mock`；至少避免 500。

### P1-3 审计留痕丢失（非 UUID workspace 时整行丢弃）
- **文件**：`forgeflow/middleware/audit.py:85` `uuid.UUID(workspace_id) if workspace_id else None`
- **复现**：带非 UUID workspace/tenant claim 的任意租户域请求（如 `/experiences`、`/skills`、`/skill-candidates`）
- **现象**：日志 `ERROR forgeflow.middleware.audit — AUDIT_WRITE_FAILED request_id=… badly formed hexadecimal UUID string`（一次测试出现 8+ 次），整条 `INSERT INTO audit_log` 失败 → **审计记录静默丢失**
- **根因**：`user_id` 有 `_is_uuid` 形状守卫（audit.py:79），`workspace_id` 没有
- **修复建议**：对 `workspace_id` 复用 `_is_uuid` 守卫，非法时写 `NULL`，避免整行丢失。

### P2-1 HITL 未接入运行时执行路径（见 V6）
### P2-2 "SSE 断流降级为轮询"仅 UI 文案（见 V9.4）
### P2-3 顶部栏铃铛无红点；组织切换器硬编码"演示工作区 · Acme / prod-us-east-1"（见 V9.3）
### P3-1 memory 后端 `/metrics/` 摘要不可用（`MetricsStore.get_summary failed: AsyncMock.keys() …`），返回空指标（离线 demo 可接受）

---

## 12. 遗留风险

1. **HITL 阻断链路未闭环**：运行时未调用 PolicyEngine，真实高风险工具调用不受拦（V6）。P0 验收未要求，但属安全设计缺口。
2. **PII 类别不全**：仅覆盖美式 SSN，中国身份证漏检（P1-1）。
3. **新旧 memory 路径不一致**：新旧仓储/embeddings 混用，导致离线模式部分端点 500（P1-2）；`/memory/search` 与 `/memory` 行为分叉。
4. **审计可用性**：审计写入为 best-effort 且对非 UUID workspace 会整行失败（P1-3），SECURITY_AUDIT §6"不可篡改审计"在异常场景存在缺口。
5. **无 PG 环境未覆盖**：所有 DB 迁移/PG 仓储路径仅经 offline SQL 校验，未在真实 PG 上回归（本机限制）。

---

## 13. 复现命令汇总（供工程师）

```powershell
$py = "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
$env:STORAGE_BACKEND="memory"; $env:LLM_PROVIDER="mock"; $env:EMBEDDING_PROVIDER="mock"
# V2 基线
& $py -m pytest tests/ -q
# V3/V5/V6/V7/V8 端到端（本报告主证据脚本）
& $py "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\.qa-tmp\qa_verify.py"
# V4 路由门禁
& $py "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\.qa-tmp\qa_v4.py"
# V9.4 SSE
& $py "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\.qa-tmp\qa_sse.py"
```

> 证据文件目录：`D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\.qa-tmp\`
> （`qa_verify_out.txt`、`qa_v4_out.txt`、`qa_sse_out.txt`、`qa_pytest_full.txt`、`frontend_build2.txt`、`alembic_offline.sql`、`alembic_down.sql`、`tests_grouped.txt`）

---

## 14. Round 2 回归（工程师修复后）— 判定 `NoOne`

工程师回传"P1 全修 + P2 一并处理，407 passed"。QA **独立复核**（不信任其结论，逐项自测）：

### 14.1 完整回归 — ✅
```
collected 410 items
================ 407 passed, 3 skipped, 44 warnings in 19.44s ================
EXIT=0
```
3 skipped 仍为 `tests/integration/test_auth_db.py`（live PG 不可达），无新增跳过。新增测试：`test_memory_offline_search.py`(集成1)、`test_memory_offline_embedding.py`(3)、`test_runtime_gate.py`(2)，抽读均为**实断言**（非 tautology）。

### 14.2 P1-1 身份证 PII — ✅ 已修（自测）
- 代码：`forgeflow/security/pii_redactor.py:35-38` 新增 `_CN_ID_RE`，并在 patterns 中置于 phone/ssn/credit_card 之前（:88-90）。
- 自测（`.qa-tmp/qa_r2_out.txt` R2-A）：
  ```
  scan[cn_id_18]    pii=True cats=['cn_id'] redacted='身份证 [REDACTED:cn_id]'
  scan[cn_id_X]     pii=True cats=['cn_id']
  scan[cn_id_lowerx] pii=True cats=['cn_id']
  scan_outbound(http, cn_id only) blocked=True reason=outbound PII blocked: cn_id
  scan[email] pii=True   scan[phone] pii=True        # 旧类别无回归
  ```
  18 位含 X/x 校验位均命中；仅含 cn_id 的外发被拦截。

### 14.3 P1-2 `/memory/search` 离线 500 — ✅ 已修（自测）
- 代码：`forgeflow/memory/pgvector_store.py:22-63` 新增 `_OfflineEmbeddings`，`_get_embeddings()` 按 `EMBEDDING_PROVIDER` 分支（非 openai 不构造 `OpenAIEmbeddings`）。
- 自测：`GET /memory/search?q=hello -> 200`（`.qa-tmp/qa_r2_out.txt` R2-C 与 `qa_r2_verify.txt:214`），body=`[]`，不再 `OpenAIError`。

### 14.4 P1-3 审计丢行 — ✅ 已修（自测）
- 代码：`forgeflow/middleware/audit.py:29-38,91,99` 抽出 `_uuid_or_none()`，`user_id`/`workspace_id` 均归一。
- 自测：重跑全链路 E2E（含 `/experiences`、`/skills`、`/skill-candidates` 等租户域请求），日志**不再出现任何** `AUDIT_WRITE_FAILED`（`qa_r2_verify.txt` 全文 grep 零命中）。

### 14.5 P2-V6 运行时策略闸门 — ✅ 已修（自测）
- 代码：`forgeflow/runtime/orchestrator.py:121-181` 每个 step 调 `engine.evaluate_tool_call()`，高风险→`run.error` 且**在工具执行前 return**；`governance/policy_engine.py:34-45,57-58,208-274` 新增 `_intent_is_sensitive`、`sensitive` 分支与 `evaluate_tool_call`。
- 自测（`.qa-tmp/qa_r2_out.txt` R2-B/R2-D）：
  ```
  POST /tasks(敏感intent "删除生产数据库并清空所有记录") -> 403  high-risk task requires human approval
  POST /tasks(benign) -> 200 completed                     # 未误伤
  high-risk tool payment.transfer -> status=failed steps=[] run.error emitted=True, approvals pending=4
  low-risk default plan          -> status=completed steps=3
  ```

### 14.6 P2-V9 前端 — ✅ 已修（代码 + 构建自测）
- `hooks/useRunEvents.ts:99-133` 断流时 `onError` **真正**启动 `hubApi.runDetail(runId)` 每 2s 轮询并重建时间线，终态停止（不再是纯文案）。
- `components/Topbar.tsx:40-48,94-124` 工作区标签改由 `useSession()` 派生（去掉 Acme/prod-us-east-1 硬编码）；铃铛新增 `useHubApprovals({status:'pending'})` 红点计数。
- 完整构建（非仅 tsc）：`npm run build` → `✓ built in 4.59s`，**EXIT=0**（`.qa-tmp/frontend_build_r2.txt`）。

### 14.7 Round 2 结论
全部 Round 1 缺陷（3×P1 + 额外 1×P1）+ 2 个 P2 均已独立复核通过；无新增缺陷、无回归。**判定 `NoOne`（全部通过）。** 2 轮上限内收敛。

**遗留（非阻断、已知）**：
1. `/memory/search` 虽恢复 200，但仍走 legacy `PGVectorStore` 路径，未并入新 Repository 层（架构一致性，P0 可接受）。
2. memory 后端 `/metrics/` 摘要仍返回空（P3，既有）。
3. 运行时闸门仅评估风险、不重复 RBAC/ABAC（设计取舍：入口已过 RBAC），可接受。
4. 仓库根残留工程师临时 `_*.txt`（无害，建议清理）。
