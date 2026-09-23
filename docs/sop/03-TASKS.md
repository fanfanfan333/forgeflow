# AgentFlow · 任务分解（03-TASKS）

> 版本：v1.0 ｜ 负责人：高见远（架构师）
> 配套：`docs/sop/02-ARCHITECTURE.md`（设计） ｜ `docs/sop/01-PRD.md`（需求）
> 排序原则：**底层 → 上层**（配置 → Repository → 核心服务 → API → SSE → 前端 → 文档）。每任务 1–3 个文件。
> 路径均相对仓库根 `ForgeFlow-main/`，除非特别标注。

---

## 1. 任务表（按实现顺序排列）

| 任务号 | 标题 | 目标文件 | 依赖 | 产出物 | 完成判据（可验证） | 优先级 |
|---|---|---|---|---|---|---|
| **T01** | 基础设施与配置扩展 | `forgeflow/config.py`、`forgeflow/models/provider.py`、`.env.example` | — | Settings 新增字段（§10.3）+ `get_model()` 的 `mock` 分支 | `LLM_PROVIDER=mock python -c "from forgeflow.models import get_model; print(get_model())"` 不报错；`pytest tests/unit/test_provider.py` 全绿；`STORAGE_BACKEND`/`EMBEDDING_PROVIDER` 等新字段可读 | P0 |
| **T02** | Repository 抽象层 | `forgeflow/repositories/__init__.py`、`repositories/base.py`、`repositories/factory.py` | T01 | `Protocol` 定义 + `TenantScopedRepository` 基类 + `get_*_repository()` 工厂 | import 成功；`STORAGE_BACKEND=memory` 下 `get_experience_repository()` 返回内存实现且不触 PG；`pytest` 无新增报错 | P0 |
| **T03** | 领域模型 + 内存/PG 仓储 | `forgeflow/experience/models.py`、`repositories/memory/experience_repo.py`、`repositories/postgres/experience_repo.py` | T02 | `ExperienceRecord` 等 dataclass + 两套仓储实现 | 内存仓储单测通过：`save`/`list`/`get`/`find_similar`（余弦）+ N:M 关联；接口与 PG 实现签名一致 | P0 |
| **T04** | Alembic 迁移 010（13 实体 DDL） | `alembic/versions/010_agentflow_hubs.py` | T01 | 迁移脚本（`revision="010"`, `down_revision="009"`） | 有 PG 时 `alembic upgrade head` 建表成功、`downgrade` 可逆；无 PG 时模块可 import（不触发连接） | P0 |
| **T05** | Validation Layer | `forgeflow/validation/__init__.py`、`validation/validator.py`、`validation/replan.py` | T03 | `validate(run)->Verdict` + Replan 触发 | 单测：成功→`Verdict(success)`；失败→产出 `replan` 事件；超 `MAX_REPLAN_ATTEMPTS`→转 HITL 标记 | P0 |
| **T06** | Experience 提取 + 五层 Memory scope | `forgeflow/experience/__init__.py`、`experience/extractor.py`、`experience/scopes.py` | T03 | `ExperienceExtractor` + scope 枚举（User/Team/Episodic/Semantic/Org） | 单测：终态 run → 生成 `Experience`（含 summary/decisions/outcome/reusable_steps），并写入 `experience_memory` N:M | P0 |
| **T07** | Agent Runtime 编排 + 事件总线 | `forgeflow/runtime/__init__.py`、`runtime/orchestrator.py`、`runtime/events.py` | T05 | `run_task()` + `RunEventBus` | 单测：`run_task` 经 mock 图跑到终态；事件按序产出；**未修改** `graph/builder.py`（`test_graph.py` 仍绿） | P0 |
| **T08** | Skill Registry + Versioning | `forgeflow/skills/__init__.py`、`skills/registry.py`、`skills/versioning.py` | T03 | Registry + semver/changelog/diff/rollback | 单测：register → create_version(semver) → list → rollback → diff 全通过 | P0 |
| **T09** | Skill Candidate Compiler（缺失的一环） | `forgeflow/skills/candidate_compiler.py`、`skills/draft_spec.py` | T06, T08 | `compile_candidate()` + `DraftSpec` | 单测：≥N 相似经验→产出 `SkillCandidate`（`draft_spec` 四要素非空）；<N→`insufficient`；`LLM_PROVIDER=mock` 走规则兜底仍成功 | P0 |
| **T10** | 评估 + 治理闸门 | `forgeflow/skills/evaluator.py`、`skills/governance_gate.py` | T09, T11 | `evaluate_candidate()` + `promote_candidate()` | 单测：未评估→promote 拒绝；评估通过 + 审批→生成 `SkillVersion(approved_by)`；无审批→403/待批 | P0 |
| **T11** | Security Hub：Policy / Tenant / DLP | `forgeflow/governance/__init__.py`、`governance/policy_engine.py`、`governance/tenancy.py`、`governance/dlp.py` | T02 | `PolicyEngine` + `TenantIsolation` + `DlpGate` | 单测：越权→deny(403)；high risk→`requires_approval=True`；PII 外发→拦截并告警 | P0 |
| **T12** | API 路由（任务/运行/经验/记忆） | `forgeflow/api/routers/tasks.py`、`routers/experiences.py`、`routers/memory.py`(扩展) | T07, T11 | 任务/运行/经验/记忆 REST | `POST /api/tasks` 建 run；`GET /api/experiences` 列表；`GET /api/experiences/{id}/lineage` 追溯；旧 `/memory/store`、`/memory/search` 不破 | P0 |
| **T13** | API 路由（技能/候选/策略/审批）+ RBAC 登记 | `forgeflow/api/routers/skills.py`、`routers/policies.py`、`forgeflow/rbac/policies.py`、`forgeflow/api/main.py` | T09, T10, T11 | 技能/候选/策略/审批 REST + 权限登记 | 新路由**全部**登记 `ROUTE_PERMISSION_MAP`（否则 fail-closed）；`GET /api/skills` 返回列表；`admin`(`*:*`) 可通，无 token→401 | P0 |
| **T14** | SSE 实时执行流端点 | `forgeflow/api/routers/runs.py`、`runtime/events.py`(SSE 桥接) | T07 | `GET /runs/{id}/events`(SSE) | 建连 ≤1s；步骤实时追加；`[DONE]` 收尾；断线重连补发；`X-Accel-Buffering: no` | P0 |
| **T15** | 前端：路由/导航/首页 | `frontend/src/router.tsx`、`components/Sidebar.tsx`、`components/AppShell.tsx`、`views/HomeView.tsx`、`styles/home.css` | T13, T14 | 10 项导航 + 首页 6 区块 | 10 项导航可切换且高亮正确；首页 4KPI/6Agent/4技能/近期任务/日志/安全概览 **全部接真实 API（无硬编码）**；深色 oklch 风格一致 | P0 |
| **T16** | 前端：数据层 + SSE Hook | `frontend/src/api/client.ts`(扩展)、`api/hooks.ts`(扩展)、`api/sse.ts`、`hooks/useRunEvents.ts` | T13, T14 | 类型化 API + `useRunEvents` | 首页数据可加载；SSE 日志实时追加、断线重连；SSE 不可用时降级轮询 | P0 |
| **T17** | 前端：Hub 页面 | `frontend/src/views/SkillsView.tsx`、`views/KnowledgeView.tsx`、`views/SecurityView.tsx`、`styles/skills.css` | T13 | 技能中心/知识库/安全页 | 技能中心可检索、查看版本、一键固化候选；安全页展示策略与待审批 | P0 |
| **T18** | 运行手册与交接文档 | `docs/sop/RUNBOOK.md`、（可选）`README` 片段 | T15 | 本机运行手册 | 按手册可在「memory + mock 全离线」与「PG 5433」两种模式启动；命令全部可复制执行 | P0 |

### 并行 / 串行标注
- **串行主干（不可并行）**：`T01 → T02 → T03 → T09 → T10`；`T03 → T05 → T07 → T14`；`T07 → T12`；`T13 → T15/T16/T17`。
- **可并行组一**：`T03 ∥ T04`（均只依赖 T01/T02）。
- **可并行组二**：`T05 ∥ T06 ∥ T08 ∥ T11`（分别依赖 T03/T02，互不阻塞）。
- **可并行组三**：`T12 ∥ T14`（均依赖 T07 + 各自前置）；`T15 ∥ T16 ∥ T17`（均依赖 T13/T14）。
- **建议执行波次**：波1 `T01`；波2 `T02, T04`；波3 `T03, T11`；波4 `T05, T06, T08`；波5 `T07, T09`；波6 `T10`；波7 `T12, T14`；波8 `T13`；波9 `T15, T16, T17`；波10 `T18`。

---

## 2. P0 关键路径（跑通闭环 + 首页的最小任务集合）

> 目标：一次成功任务 + 一次失败任务均产出 `Experience`；失败经验经 Replan 产出 `SkillCandidate`；首页 6 区块全接真实 API。

```
最小闭环关键路径（18 个 P0 任务全在此路径或其并行补全）：

  T01 ─▶ T02 ─▶ T03 ─┬─▶ T05 ─▶ T07 ─┬─▶ T12 ─▶ T13 ─▶ T15 ─▶ T18
                      │                └─▶ T14 ─▶ T16
                      ├─▶ T06 ─▶ T09 ─▶ T10 ─▶ T13
                      └─▶ T08 ─▶ T09
  T04（迁移，与 T03 并行）
  T11（Security Hub，与 T05/T06/T08 并行）─▶ T10 / T12 / T13
  T17（Hub 页，与 T15/T16 并行）

P0 关键路径任务号区间：T01 – T18（全部 P0）
其中「闭环最小集」：{T01,T02,T03,T04,T05,T06,T07,T08,T09,T10,T11,T12,T14}
其中「首页最小集」：{T01,T02,T04,T11,T12,T13,T14,T15,T16}
```

---

## 3. P1 / P2 延后任务

| 编号 | 标题 | 目标文件 | 依赖 | 对应 PRD |
|---|---|---|---|---|
| P1-a | DLP 规则可配 + ABAC 条件策略增强 | `forgeflow/governance/dlp.py`、`governance/policy_engine.py` | T11 | P1-01, P1-02 |
| P1-b | Cost Budget 预算感知降级 | `forgeflow/cost/budget.py`（新）、联动 `resilience/budget_guard.py` | T11 | P1-03 |
| P1-c | SLO 分级 + 实时达标率 | `forgeflow/observability/slo.py`、`api/routers/metrics.py` | T11 | P1-04 |
| P1-d | Context Builder（统一上下文） | `forgeflow/experience/context_builder.py` | T06 | P1-05 |
| P1-e | Organizational Memory 下沉 | `forgeflow/experience/scopes.py` | T06 | P1-06 |
| P1-f | 全链路审计与导出 | `forgeflow/api/routers/audit.py`（扩展） | T13 | P1-07 |
| P1-g | 「可信×进化」发布前基线校验 | `forgeflow/skills/governance_gate.py` | T10 | P1-08, US-14 |
| P2-a | Skill Evolution 建议 | `forgeflow/skills/evolution.py` | T10 | P2-01 |
| P2-b | Marketplace 评分/评论/排行 | `forgeflow/marketplace/registry.py`、`api/routers/marketplace.py` | T13 | P2-02 |
| P2-c | 多模型可插拔路由降级 | `forgeflow/models/provider.py` | T01 | P2-03 |
| P2-d | 记忆/经验自动去重与冲突消解 | `forgeflow/experience/extractor.py` | T06 | P2-04 |
| P2-e | 事件层扩展（Redis/Kafka 触发 Run） | `forgeflow/events/dispatcher.py`（扩展） | T07 | P2-05 |
| P2-f | 首页个性化（按角色） | `frontend/src/views/HomeView.tsx` | T15 | P2-06 |
| P2-g | 多模态输入（PDF/图片） | `forgeflow/mcp/server/tools/multimodal_tools.py`（复用） | T07 | P2-07 |

---

## 4. 验收测试清单（给 QA）

> 运行环境：`STORAGE_BACKEND=memory LLM_PROVIDER=mock`（无 PG / 无 Ollama）或 `STORAGE_BACKEND=postgres`（PG 5433）。
> 新增测试建议放 `tests/unit/`（36 个既有文件之外新增），集成测试放 `tests/integration/`。

| PRD 需求 | 对应测试（命令 / 断言） |
|---|---|
| G1 闭环（成功任务产 Experience） | `pytest tests/unit/test_experience_extraction.py -k success` → 断言生成 `Experience(outcome="success")` 且 `run_id` 指向源 Run |
| G1 闭环（失败任务 + Replan） | `pytest tests/unit/test_validation.py` → 断言失败→`replan` 事件；超限→HITL |
| P0-02 跳③ 候选编译 | `pytest tests/unit/test_candidate_compiler.py` → `compile_candidate` 在 ≥N 相似经验时返回 `draft_spec.prompt/steps/tools/io_schema` 非空；`<N` 返回 `insufficient` |
| P0-03 Skill 版本化 | `pytest tests/unit/test_skill_versioning.py` → semver 递增、rollback 后 `current_version` 回退、diff 非空 |
| P0-04 评估接入 | `pytest tests/unit/test_skill_evaluator.py` → 候选生成即产 `SkillEvaluation(verdict)` |
| P0-05 治理闸门 | `pytest tests/unit/test_governance_gate.py` → 未评估/未审批 promote → 403；通过→`SkillVersion(approved_by!=None)` |
| P0-07 Replan 上限 | `pytest tests/unit/test_validation.py -k max_attempts` → 达 `MAX_REPLAN_ATTEMPTS` 后不再 replan |
| P0-09 高风险 HITL | `pytest tests/unit/test_policy_engine.py -k high_risk` → `requires_approval=True` 且生成 `Approval` |
| P0-10 租户隔离 | `pytest tests/unit/test_tenant_isolation.py` → 跨租户查询返回空/404、写返回 403（100% 拦截用例） |
| P0-10 越权 403 | `pytest tests/integration/test_api.py -k skills` → 无 token→401；`viewer` 写 skill→403 |
| P0-13 五层 Memory | `pytest tests/unit/test_memory_scopes.py` → 按 scope 分区写入/读取隔离 |
| P0-08 MCP 输出防护 | `pytest tests/unit/test_security.py -k tool_output` → 注入内容被包裹 `<UNTRUSTED_TOOL_OUTPUT>` 或 HIGH 脱敏 |
| P0-12 SSE | `pytest tests/integration/test_runs_sse.py` → 建连后收到 ≥1 条 `data:` 事件并以 `[DONE]` 收尾 |
| P0-11 首页真实数据 | 手工：`GET /api/metrics/`、`/api/agents/`、`/api/skills?featured=true`、`/api/security/overview` 均 200 且首页渲染对应区块（无硬编码） |
| C3 存量兼容 | `pytest tests/`（43 个既有文件）**全绿**；`pytest tests/unit/test_graph.py tests/unit/test_rbac.py tests/unit/test_multi_tenant.py tests/unit/test_memory.py` 不得回归 |
| C2 零依赖可跑 | `STORAGE_BACKEND=memory LLM_PROVIDER=mock python -c "import forgeflow.api.main"` 不报错；`pytest tests/unit` 全绿 |

**关键：C3 回归门禁** —— 合并前必须保证 `pytest tests/` 基线 43 文件全绿；任何新增路由必须同步登记 `ROUTE_PERMISSION_MAP`，否则运行期 403（fail-closed）。

---

> 交接说明：请工程师从 **T01** 开始按表串行/并行执行；每个任务完成后跑该行「完成判据」。P0 全部完成后进入 `T18` 运行手册与最终验收。
