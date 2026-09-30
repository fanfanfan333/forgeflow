# AgentFlow · 增量任务列表（INC2）— 06-TASKS-INC2

> 2026-09-26 INC16：OpenAI/Anthropic provider 已移除，本节为历史记录。

> 版本：v1.0 ｜ 负责人：高见远（架构师）
> 配套：`docs/sop/05-ARCHITECTURE-INC2.md`（设计） ｜ `docs/sop/04-QA-REPORT.md`（遗留）
> 粒度：每任务 **1–3 个文件**。路径相对仓库根 `ForgeFlow-main/`。
> 约定：新路由由 **INC2-08 汇点任务**统一在 `api/main.py` 挂载 + `rbac/policies.py` 登记（避免单文件冲突、保证 fail-closed）。

---

## 1. 任务表（按依赖排序）

| 任务号 | 标题 | 目标文件 | 依赖 | 产出物 | 完成判据（可验证） | 优先级 |
|---|---|---|---|---|---|---|
| **INC2-01** | 配置层扩展 | `forgeflow/config.py`、`.env.example` | — | 新 settings：`cost_*`、`slo_*`、`context_budget_tokens`、`dedup_merge_threshold`、`model_fallback_chain`、`ollama_think`、`marketplace_cross_tenant`、`multimodal_max_bytes`、`dlp_rules_file` | `python -c "from forgeflow.config import get_settings; s=get_settings(); print(s.model_fallback_chain, s.context_budget_tokens)"` 不报错；`.env.example` 与 config 字段一一对应；`pytest tests/unit/test_provider.py` 仍绿 | P0 |
| **INC2-02** | 迁移 011（Cost/SLO/Evolution 表与列） | `alembic/versions/011_cost_slo_evolution.py` | INC2-01 | `cost_budgets`、`skill_ratings`、`context_build_stats`；加列 `skill_listings`/`experiences`/`skills`/`agent_approvals` | 真 PG：`alembic upgrade head` 成功建表；`alembic downgrade 011:010` 对称；模块 import 不连 DB；**未改 010** | P0 |
| **INC2-03** | Cost 仓储（内存 + PG） | `forgeflow/cost/__init__.py`、`repositories/memory/cost_repo.py`、`repositories/postgres/cost_repo.py` | INC2-02 | 双实现 `CostBudgetRepository`（get/upsert/list by scope） | memory 仓储单测 CRUD 通过；真 PG 仓储 upsert+list 通过；无 PG 时 import 不报错 | P0 |
| **INC2-04** | 仓储接线 | `forgeflow/repositories/base.py`、`forgeflow/repositories/factory.py` | INC2-03 | 追加 `CostBudgetRepository` Protocol + `kind="cost"`/`kind="memory"` 工厂分支 | `get_cost_repository()` 在 memory/postgres 两档均可取；`reset_repositories()` 后重取一致 | P0 |
| **INC2-05** | Cost 服务（预算 / 降级 / 节省） | `forgeflow/cost/budget_service.py`、`forgeflow/cost/degrade.py`、`forgeflow/cost/savings.py` | INC2-04 | 三级预算、`evaluate_budget`、降级动作、节省计算 | 单测：80%→`warn`、100%→`exceeded` 且动作序列正确；`savings` 相对基线计算正确；无 LLM 依赖 | P0 |
| **INC2-06** | metrics 内存数据源（D2） | `forgeflow/observability/metrics_source.py`、`forgeflow/observability/metrics_store.py`、`forgeflow/api/routers/metrics.py` | INC2-01 | `MetricsSource` 抽象 + memory(读 run store) / postgres 两实现 | memory 后端 `GET /metrics/` 返回**非空**（run store 聚合）；PG 后端 `/metrics/*` 结果与改造前一致（回归）；`/cost`、`/runs` 同 | P0 |
| **INC2-07** | SLO 分级 + 达标率 | `forgeflow/observability/slo.py`、`forgeflow/api/routers/metrics.py`（追加 `/slo`） | INC2-06、INC2-05 | `SLO_TIERS`+`SloRegistry`+`GET /slo` | 单测：达标率计算正确；`breaching=True` 时触发对应 `degrade` 回调；`GET /metrics/slo` 返回三级 | P0 |
| **INC2-08** | API 挂载 + RBAC 登记（汇点） | `forgeflow/api/main.py`、`forgeflow/rbac/policies.py` | INC2-05、INC2-07、INC2-12、INC2-14、INC2-20、INC2-24 | 挂载所有新 router + 全量 `ROUTE_PERMISSION_MAP` 登记 + `ROLE_PERMISSIONS` 新权限 | `openapi()` 全量 (method,path) 差集为空；无 token→401；`admin` 通过；viewer 写→403 | P0 |
| **INC2-09** | Context Builder（A5） | `forgeflow/experience/context_builder.py`、`forgeflow/experience/token_budget.py` | INC2-01 | `build_context()` + token 估算 | 单测：按 `budget_tokens` 返回 bundle；去重生效；`compression_ratio`/`hit_rate` 有值；无 LLM 依赖 | P0 |
| **INC2-10** | Context 接入运行时 + 统计 | `forgeflow/runtime/orchestrator.py`、`forgeflow/observability/context_stats.py` | INC2-09 | 运行时经 builder 取上下文；统计记录 | `run_task` 的 `ctx` 来自 builder（`available_skills` 非空）；统计落内存/PG；既有 runtime 用例不回归 | P0 |
| **INC2-11** | DLP 规则可配（B1） | `forgeflow/governance/dlp_rules.py`、`forgeflow/governance/dlp.py` | INC2-01 | `DlpRuleSet`（默认集 + JSON 覆盖） | 单测：自定义规则生效；默认仍拦 `cn_id`/`email`；`DLP_ENABLED=false` 时不 block | P0 |
| **INC2-12** | Marketplace 技能级（A4 核心） | `forgeflow/skills/marketplace_bridge.py`、`forgeflow/api/routers/marketplace.py` | INC2-11 | 上架(含 DLP)/检索/安装/评分 + `skill_listings` | 上架前 DLP 命中→403；检索（关键词+领域）命中；install 到本租户生成 Skill；`shared=False` 默认 | P0 |
| **INC2-13** | Skill 可信基线（A7） | `forgeflow/skills/trust_baseline.py`、`forgeflow/skills/governance_gate.py` | INC2-11 | `verify_trust_baseline()` 挂入 promote | 单测：工具白名单/越权/PII 任一失败→发布被拒(403)；全通过→放行；既有 promote 用例不回归 | P0 |
| **INC2-14** | `/memory/search` 归并（D1）+ A6 提升端点 | `forgeflow/experience/memory_store.py`、`forgeflow/api/routers/memory.py` | INC2-01、INC2-15 | `memory_store.search()` + `POST /memory/{id}/promote` | memory 后端 `GET /memory/search?q=` 200（不再依赖 PG/OpenAI）；PG 后端响应结构 `MemorySearchResult[]` 不变；promote 写审计 | P0 |
| **INC2-15** | Org Memory 下沉/提升（A6） | `forgeflow/experience/promotion.py` | INC2-01 | `promote_memory()`（规则 + 审计） | 单测：`promotable=False` 拒绝；→org 需 admin；namespace 重算正确 | P0 |
| **INC2-16** | 去重/冲突消解（B4） | `forgeflow/experience/dedup.py`、`forgeflow/experience/extractor.py` | INC2-02 | 相似合并 / 冲突标记 + 批量仲裁 | 单测：≥`dedup_merge_threshold` 合并（不新增行、`merged_from` 累加）；冲突带标 `conflict_with`；embedding 为主、LLM 一次批量 | P1 |
| **INC2-17** | ABAC 条件表达式（B2） | `forgeflow/governance/conditions.py`、`forgeflow/governance/policy_engine.py` | INC2-01 | 条件算子（in/ne/gt/lt/contains/all/any） | 单测：新算子求值正确；**旧式 `key==value` 语义不变**（`test_policy_engine.py` 不回归）；决策可审计 | P1 |
| **INC2-18** | 审计导出（B3） | `forgeflow/api/routers/audit.py`、`forgeflow/middleware/audit.py` | INC2-01 | `GET /audit/export`（csv/json）+ 内存缓冲 | memory 后端可导出（内存缓冲）；PG 后端按租户/时间导出；权限 `read:audit` | P1 |
| **INC2-19** | Skill Evolution（A3） | `forgeflow/skills/evolution.py` | INC2-08 | `advise()`：optimize/retire + 依据 | 单测：规则筛选产出建议 + 依据指标；**仅 1 次批量 LLM**（mock 下走规则）；结果缓存 | P1 |
| **INC2-20** | Evolution API | `forgeflow/api/routers/skills.py` | INC2-19、INC2-08 | `GET /skills/evolution-advice` | 返回建议列表；`approval_required=true`；权限 `read:skills` | P1 |
| **INC2-21** | 运行时 RBAC 复核（B5） | `forgeflow/runtime/gate.py`、`forgeflow/runtime/orchestrator.py` | INC2-10 | `check_tool_permission()` 接入 step | 单测：越权工具→`run.error` 且执行前 return；合法工具不受影响（QA 遗留项闭环） | P1 |
| **INC2-22** | 多模型降级路由（C1） | `forgeflow/models/provider.py` | INC2-01 | 降级链 strong→weak→mock | 单测：Ollama 不可用时不抛错、返回可用模型/ mock；降级链不选 thinking 模型 | P1 |
| **INC2-23** | 事件→Run（C2） | `forgeflow/events/dispatcher.py` | INC2-10 | `{task:{intent}}` envelope 触发 run_task | 单测：新 envelope 派发到 `run_task`；旧三类 workflow 分支不变（`test_events.py` 不回归） | P2 |
| **INC2-24** | 多模态进任务（C4） | `forgeflow/runtime/attachments.py`、`forgeflow/api/routers/tasks.py` | INC2-10 | 附件预处理注入任务上下文 | pdf 文本注入 intent；可选依赖缺失→降级"附件已忽略"不报错；`POST /tasks` 体积上限 | P2 |
| **INC2-25** | 前端：Cost 看板 + Ops SLO 页 | `frontend/src/views/CostView.tsx`、`frontend/src/views/OpsView.tsx`、`frontend/src/api/client.ts` | INC2-08 | 成本看板 + SLO 达标率页 | 成本看板接 `/cost/board`、`/cost/savings`；Ops 页接 `/metrics/slo`；`vite build` 零报错 | P1 |
| **INC2-26** | 前端：首页个性化 + KPI「节省成本」+ 硬编码清理 | `frontend/src/views/HomeView.tsx`、`frontend/src/api/hooks.ts`、`frontend/src/home/roleConfig.ts`、`frontend/src/components/Topbar.tsx` | INC2-08 | 按角色差异化 + KPI#3 接 savings + 清硬编码 | 不同 role 渲染差异化 KPI/Chip；KPI#3 显示 `/cost/savings` 真实值（非累计成本）；**清除残留 `Acme`/`prod-us-east-1` 硬编码**（`grep -r "Acme\|prod-us-east-1" frontend/src` 零命中）；`vite build` 零报错 | P1 |
| **INC2-27** | 文档与运行手册 | `docs/sop/07-RUNBOOK-INC2.md` | INC2-08 | INC2 运行手册（真 PG/真模型 + 降级档） | 按手册可启动（真环境 + memory+mock 全离线两档）；命令可复制 | P2 |

---

## 2. 并行波次（哪些可同时做）

| 波次 | 任务 | 说明 |
|---|---|---|
| **W1** | INC2-01 | 配置先行（一切新字段之源） |
| **W2** | INC2-02 ∥ INC2-06 ∥ INC2-09 ∥ INC2-11 ∥ INC2-14 | 仅依赖 01；06/09/11 相互独立（不同文件）；14 需 15 |
| **W3** | INC2-03 ∥ INC2-07 ∥ INC2-10 ∥ INC2-12 ∥ INC2-13 ∥ INC2-15 ∥ INC2-16 ∥ INC2-17 ∥ INC2-18 ∥ INC2-19 ∥ INC2-22 | 依赖 W2 产物；文件互不重叠可并行 |
| **W4** | INC2-04 ∥ INC2-20 ∥ INC2-21 ∥ INC2-23 ∥ INC2-24 | 依赖 W3（04←03、20←19、21←10、23←10、24←10） |
| **W5** | INC2-05 ∥ INC2-14(收尾) | 05←04 |
| **W6** | **INC2-08**（汇点，唯一改 `main.py`+`rbac/policies.py`） | 需 05/07/12/14/20/24 均完成 |
| **W7** | INC2-25 ∥ INC2-26 ∥ INC2-27 | 前端与文档，需 08 |

**串行强制链**：
- `INC2-01 → INC2-02 → INC2-03 → INC2-04 → INC2-05 → INC2-08`
- `INC2-01 → INC2-06 → INC2-07 → INC2-08`
- `INC2-01 → INC2-09 → INC2-10 → {INC2-21, INC2-23, INC2-24}`
- `INC2-01 → INC2-15 → INC2-14 → INC2-08`
- `INC2-01 → INC2-11 → {INC2-12, INC2-13}`
- `INC2-19 → INC2-20 → INC2-08`
- `INC2-08 → {INC2-25, INC2-26}`

**冲突规避**：`rbac/policies.py` 与 `api/main.py` **仅 INC2-08 触碰**；`api/routers/metrics.py` 由 06→07 串行；`runtime/orchestrator.py` 由 10→21 串行；`experience/memory_store.py` 由 14 独占（15 只加 `promotion.py`）。

---

## 3. 验收测试清单（给 QA）

> 环境：**memory+mock** 档 = `STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock`；**真环境** 档 = 真 PG（5433）+ Ollama `qwen3:8b`（`reasoning=False`）。
> 命令用 `C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe`。

### 3.1 必须真 PG 验证
| 项 | 命令 / 断言 |
|---|---|
| 迁移 011 | `alembic upgrade head` 成功；`alembic downgrade 011:010 --sql` 与 `upgrade 010:011 --sql` 对称；表数一致 |
| Cost PG 仓储 | `pytest tests/unit/test_cost_repo_pg.py`（真 PG fixture）→ upsert/list 通过 |
| D1 PG 响应结构不变 | 真 PG 下 `GET /memory/search?q=hello` → 200 且 body 元素含 `id/content/similarity/namespace/metadata`（与旧一致） |
| B4 新列 | 真 PG 下 `INSERT` 一条 experience 带 `conflict_with/merged_from` 成功 |
| B3 导出 | 真 PG 下 `GET /audit/export?format=csv&from=…` → 200 `text/csv`，行数与 `/audit/search` 一致 |
| metrics PG 不回归 | 真 PG 下 `/metrics/`、`/metrics/cost`、`/metrics/runs` 输出与改造前一致（快照对比） |

### 3.2 必须真模型（Ollama）验证
| 项 | 命令 / 断言 |
|---|---|
| C1 降级 | 停 Ollama → `get_model()` 不抛错、返回 mock；恢复后走 `qwen3:8b`（`reasoning=False`，`content` 非空） |
| A3 Evolution | `POST/GET /skills/evolution-advice` 在真模型下**总耗时 < 30s**（证明单次批量调用，非逐条）；mock 下走规则且 < 1s |
| C4 多模态 | 传 PDF base64 → 任务上下文含抽取文本；`qwen2.5vl:3b` 在线时图片可描述，离线时降级不报错 |

### 3.3 memory+mock 即可验证
| 缺口 | 命令 / 断言 |
|---|---|
| A1 Cost | `pytest tests/unit/test_cost_degrade.py` → 80% warn / 100% exceeded + 动作序列；`test_cost_savings.py` 相对基线正确 |
| A2 SLO | `pytest tests/unit/test_slo.py` → 三级达标率；`breaching` 触发降级回调；`GET /metrics/slo` 200 |
| A5 Context | `pytest tests/unit/test_context_builder.py` → 预算裁剪、去重、`compression_ratio`/`hit_rate`；`GET /context/build` 200 |
| A6 Org Memory | `pytest tests/unit/test_memory_promotion.py` → 规则与 admin 限制；`POST /memory/{id}/promote` 200 + 审计 |
| A7 可信基线 | `pytest tests/unit/test_trust_baseline.py` → 白名单/越权/PII 三类拒绝 |
| B1 DLP 规则 | `pytest tests/unit/test_dlp_rules.py` → 自定义规则生效；默认仍拦 `cn_id`/`email` |
| B2 ABAC | `pytest tests/unit/test_conditions.py` → 新算子；**旧 `key==value` 不回归** |
| B5 运行时复核 | `pytest tests/unit/test_runtime_gate.py` → 越权工具被拦、执行前 return |
| C2 事件→Run | `pytest tests/unit/test_events.py -k task` → 新 envelope 派发；旧分支不变 |
| C3 前端个性化 | 手工：不同 role 登录看差异化 KPI/Chip；`vite build` 零报错 |
| D2 内存 metrics | memory 档 `GET /metrics/` 返回非空 summary；`/metrics/runs` 非空 |
| 门禁/兼容 | `openapi()` 全量 (method,path) − `ROUTE_PERMISSION_MAP` = ∅；`pytest tests/`（memory 档）≥ 410 passed；真 PG 档 ≥ 407（≤3 脚手架失败，不增） |

**合并门禁**：① memory 档 `pytest tests/` 全绿（≥410）；② 真 PG 档不新增失败（≤3 既有脚手架）；③ `openapi()` 零未登记路由；④ 前端 `vite build` 零报错。

---

## 4. 缺口 → 任务号映射（A1–A7 / B1–B5 / C1–C4 / D1–D2）

| 缺口 | 落点（文件） | 任务号 |
|---|---|---|
| **A1** Cost（三级预算 + 80%/100% 阈值 + 降级 + 节省 + 首页 KPI） | `forgeflow/cost/*`、`repositories/*/cost_repo.py`、`api/routers/cost.py` | INC2-03/04/05 + INC2-26(KPI) |
| **A2** SLO 分级（平台/租户/任务）+ 实时达标率 + 超阈值降级 | `observability/slo.py`、`api/routers/metrics.py` | INC2-07 |
| **A3** Skill Evolution（优化/退役建议） | `skills/evolution.py`、`api/routers/skills.py` | INC2-19/20 |
| **A4** 技能级 Marketplace（上架/跨团队安装/评分排行） | `skills/marketplace_bridge.py`、`api/routers/marketplace.py` | INC2-12 |
| **A5** Context Builder（检索/压缩/择优 + token 预算 + 压缩率/命中率） | `experience/context_builder.py`、`experience/token_budget.py`、`observability/context_stats.py` | INC2-09/10 |
| **A6** Org Memory 跨层提升（显式操作 + 审计） | `experience/promotion.py`、`api/routers/memory.py` | INC2-15/14 |
| **A7** 发布前可信基线（工具白名单/越权/PII） | `skills/trust_baseline.py`、`skills/governance_gate.py` | INC2-13 |
| **B1** DLP 规则可配 | `governance/dlp_rules.py`、`governance/dlp.py` | INC2-11 |
| **B2** ABAC 条件表达式增强 | `governance/conditions.py`、`governance/policy_engine.py` | INC2-17 |
| **B3** 审计按租户/时间导出 | `api/routers/audit.py`、`middleware/audit.py` | INC2-18 |
| **B4** 记忆/经验去重与冲突消解 | `experience/dedup.py`、`experience/extractor.py` | INC2-16 |
| **B5** 运行时工具调用 Policy + Cost 双闸门（含 RBAC 复核） | `runtime/gate.py`、`runtime/orchestrator.py` | INC2-21 |
| **C1** 多模型可插拔路由降级 | `models/provider.py`、`config.py` | INC2-22 |
| **C2** 事件层驱动 Run | `events/dispatcher.py` | INC2-23 |
| **C3** 首页按角色个性化 | `frontend/src/home/roleConfig.ts`、`views/HomeView.tsx` | INC2-26 |
| **C4** 多模态输入进任务 | `runtime/attachments.py`、`api/routers/tasks.py` | INC2-24 |
| **D1** `/memory/search` 归并新仓储（主理人 §三原定义） | `experience/memory_store.py`、`api/routers/memory.py` | INC2-14 |
| **D1′** 清理 `Acme`/`prod-us-east-1` 硬编码（主理人 nudge 修正定义） | `frontend/src/components/Topbar.tsx`、`views/HomeView.tsx` | INC2-26 |
| **D2** memory 后端 `/metrics/` 空 → 统一指标源（主理人 §三原定义） | `observability/metrics_source.py`、`observability/metrics_store.py`、`api/routers/metrics.py` | INC2-06 |
| **D2′** 首页 KPI 真实数据源收敛（nudge 修正定义，与工程师同口径） | 同 D2：`observability/metrics_source.py` + 前端只读 | INC2-06 + INC2-26 |

> ⚠️ **D1/D2 双定义待核对**：主理人 §三原表与后续 nudge 对 `D1`/`D2` 的指代不同（原=后端 `/memory/search` 与 `/metrics/` 内存源；nudge=前端硬编码清理与 KPI 口径）。本表**两者都覆盖**，请核对最终命名。

---

## 5. 交付边界一句话

本轮补齐 **18 项缺口**（A1–A7 / B1–B5 / C1–C4 / D1–D2）→ **27 个任务**，P0 关键路径 `INC2-01→02→03→04→05→08` 与 `01→06→07→08`；所有 LLM 相关能力**批量+缓存+规则兜底**；新路由统一由 `INC2-08` 登记；`011_*` 只增不破。
