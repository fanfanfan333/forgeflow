# AUDIT-QA-REPORT — ForgeFlow 全方位测试体检

- 被测 HEAD：`71bb307`（代码根 `D:/Agentxm/Multi-Agent/ForgeFlow-main`；git 仓库根为其上一级 `D:/Agentxm/Multi-Agent`）
- 执行人：严过关（software-qa-engineer）
- 解释器：`C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe`（3.13.14）
- 所有命令均在 `ForgeFlow-main/` 下执行（`env_file=".env"` 相对 cwd 解析）
- 原始证据目录：`qa_tmp/`（`audit_` 前缀）
- 说明：本任务为只读 + 新增证据文件，未改动任何源码/测试代码，未做任何 git 写操作。

---

## 0. 环境事实（前置，影响后文口径）

- **PG 容器拓扑与工单假设不符（如实记录，未篡改）**：`docker ps` 实况（证据 `qa_tmp/docker_ps.txt`）：
  - `forgeflow-main-postgres-1` —— `0.0.0.0:5432->5432/tcp, 0.0.0.0:5433->5432/tcp`，`Up 22 hours (healthy)`
  - `agentflow_postgres` —— 仅 `5432/tcp`（**未发布到宿主机**），`Up 9 hours`
  - 即：宿主机 `:5433` 实际由 **`forgeflow-main-postgres-1`** 提供，而非工单所述的 `agentflow_postgres`。两者都连得上、且指向**同一个库** `forgeflow`（`alembic_version='016'`，42 张表），结论不受影响。
- **Ollama 在线**：`GET http://localhost:11434/api/tags` → `models=[qwen2.5vl:3b, qwen3:8b]`。故本机**可**跑被门控的真实栈档（见 §5 附加档）。
- **`tests/` 结构**：`unit`(157 文件) / `integration`(26 文件) / **`realstack`(4 文件，默认门控关闭)**。工单未提及 `tests/realstack`，本次补测。

---

## 1. 测试矩阵表

| 档位 | 命令要点 | tests | failures | errors | skipped | 耗时(junit) | 退出码 | 证据文件 |
|---|---|---|---|---|---|---|---|---|
| 后端·离线档(memory) | `STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock pytest tests --junitxml=qa_tmp/audit_mem.xml -q` | **1548** | **0** | **0** | 15 | 168.62s | `1`（safe-delete 假信号，见 §5-E） | `qa_tmp/audit_mem.xml` / `audit_mem.log` |
| 后端·真实 PG 档(postgres) | `STORAGE_BACKEND=postgres LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock pytest tests --junitxml=qa_tmp/audit_pg.xml -q` | **1548** | **0** | **0** | 15 | 156.88s | `1`（同上假信号） | `qa_tmp/audit_pg.xml` / `audit_pg.log` |
| 前端·e2e | 重建 dist 后 `node node_modules/@playwright/test/cli.js test` | 63 | 0 | 0 | 0 | 38.3s | `0` | `qa_tmp/audit_e2e.txt`；判定源 `frontend/test-results/.last-run.json` |
| 后端·真实栈档(realstack，**附加**) | `FORGEFLOW_REAL_STACK=1 LLM_PROVIDER=ollama STORAGE_BACKEND=postgres pytest tests/realstack --junitxml=qa_tmp/audit_realstack.xml -q` | **14** | **0** | **0** | 0 | 32.74s | `0` | `qa_tmp/audit_realstack.xml` / `audit_realstack.log` |
| 后端·真实 PG 档（**主理人时点复跑**） | 同 PG 档，主理人独立复跑 | **1548** | **1** | **0** | 15 | — | `1` | `qa_tmp/_lead_full_pg2.xml`（主理人侧） |

- 两档 junit **逐文件对账**：共 287 个 `classname`，`tests/failures/errors/skipped` **四列 0 差异**（脚本见 §1-A）。mem 与 pg 差异文件清单：**空**。
- ⏱ **时点标注（关键，勿误读为“稳定全绿”）**：上表 PG 档 `failures=0` 是 **QA 采集时点**的读数；**主理人随后独立复跑全量 PG 档得到 `failures=1`**（`qa_tmp/_lead_full_pg2.xml`，红点即 §5-A 的 INC33 用例）。⇒ 「双档 0 失败」是**时点相关的一次侥幸绿，不是稳定结论**；该用例当前在**全量档已稳定复现为红**（详见 §5-A）。
- **退出码 `1` 的定性**：两次全量 stdout 末尾均出现 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":350,...}` —— 宿主机 safe-delete 守卫在 pytest 收尾清理 `D:\Temp\pytest-of-18769\garbage-*`（350 个）时杀进程。**junit 四列证明 0 失败 0 错误**；且单文件对照 `pytest tests/integration/test_api.py` → `4 passed`，`SAFE_DELETE_PROOF_EXIT=0`（证据 `qa_tmp/audit_safe_delete_proof.txt`）。故 `EXIT=1` 是**环境假信号，非测试失败**。

### 1-A. 逐文件对账脚本（可复核）
`qa_tmp/audit_coverage_map.txt` 旁：对账逻辑为解析两份 junit，按 `classname` 聚合 `tests/failures/errors/skipped` 后取差集，输出 `files total: 287 | per-file diffs: 0`。

---

## 2. E2E 结果（前端）

- **spec 数 9** / **test 数 63**（期望 ≈9 / 63，**吻合**）。9 个 spec：`console / inc26_upload / inc29_code_entries / inc32_workspace / inc33_history_role_gate / inc34_marketplace_skills / inc36_conversation / inc37_i18n / inc39_agent_completion`。
- **红绿判定只认 `frontend/test-results/.last-run.json`**，原文：
  ```json
  {
    "status": "passed",
    "failedTests": []
  }
  ```
- Playwright 汇总：`63 passed (38.3s)`；`E2E_EXIT=0`。失败清单：**空**。
- ⚠️ 复现须知（本环境坑）：`playwright.config.ts::webServer` 的 `command` 是 `npm run build && npm run preview`，而本环境 `npm` 脚本内的 `tsc` 不在 PATH（`'tsc' 不是内部或外部命令`）。首次直接跑 e2e ⇒ webServer 自启失败、0 测试执行（证据见首次失败日志）。正确姿势：**先用 `node node_modules/typescript/bin/tsc -b` + `node node_modules/vite/bin/vite.js build` 重建 dist，再以托管后台常驻 `node node_modules/vite/bin/vite.js preview --port 4173 --strictPort`**，让 `reuseExistingServer(!CI)=true` 复用新鲜 dist（否则复用旧 dist，结论失效）。

---

## 3. 构建门禁（前端）

| 步骤 | 命令 | 退出码 | 关键产物 | 证据 |
|---|---|---|---|---|
| 类型检查 | `node node_modules/typescript/bin/tsc -b` | **0** | 无输出（无类型错误） | `qa_tmp/audit_build_tsc.txt` |
| 生产构建 | `node node_modules/vite/bin/vite.js build` | **0** | `✓ 2014 modules transformed` | `qa_tmp/audit_build_vite.txt` |

- vite `v8.1.2`，`✓ built in 9.12s`。
- 构建**警告**（非失败）：部分 chunk > 500 kB（如 `DocsPage-*.js` 695 kB、`cytoscape.esm` 435 kB、`index-*.js` 332 kB）；建议按需 code-split。

---

## 4. 迁移幂等（Alembic）

- `POSTGRES_SYNC_URL=postgresql+psycopg://forgeflow:forgeflow@localhost:5433/forgeflow`（与 `.env` 一致；注意 `alembic.ini` 默认值是 `:5432`，同一容器，等价）。
- `alembic upgrade head` **连跑两次**：`RUN1_EXIT=0`，`RUN2_EXIT=0`（第二次为 no-op，无报错）。
- `alembic current` → **`016 (head)`**；`alembic heads` → `016 (head)`。版本已收敛到 head，`016_workspace_runs.py` 为最新。
- 证据：`qa_tmp/audit_alembic.txt`。

---

## 5. 归属判定（发现项分级）

> 规则：断言符合设计却拿到错输出 ⇒ 源码 Bug（报工程师）；断言本身错 ⇒ 测试缺陷（本次**不改**，仅记录）；宿主机守卫杀进程 ⇒ 环境假信号（说明机理）。

### A. 【高·状态相关的时间炸弹，且已在全量档引爆】INC33 重启历史回填在“库内 >200 行”时静默失效
- **定性升级（主理人可证伪检验证伪了我的初判）**：初判「中·仅单跑暴露的用例非 hermetic」不成立——该缺陷**在全量 PG 档同样复现为红**。原「全量 0 失败」是**时点相关的一次侥幸绿**（仅当库内晚于测试写死时间戳的行数 <200 时成立），**不是稳定结论**。
- **现象（双侧证据一致）**：
  - QA 侧单跑：`pytest tests/integration/test_inc33_history_rehydrate.py` → `1 failed, 1 passed`，`INC33_REPRO_EXIT=1`（`qa_tmp/audit_inc33_repro.txt`；integration 目录整体跑亦复现 `qa_tmp/audit_isolated_integration.txt`）。
  - 主理人侧**全量复跑**：`tests=1548 failures=1 errors=0 skipped=15`，红点同为该用例（`qa_tmp/_lead_full_pg2.xml`；单例断言失败原文 `qa_tmp/_lead_inc33_single.xml`，与 QA 侧一致）。
- **失败断言**：`tests/integration/test_inc33_history_rehydrate.py::test_history_survives_restart_and_detail_marked[postgres]` 中 `assert rehydrated is not None, "history must survive the restart (ADR-02)"` → 实得 `None`。
- **根因（已定位到符号，双方独立闭环）**：
  - 代码：`forgeflow/runtime/dispatcher.py::hydrate_run_store`（默认 `limit: int = 200`）→ `forgeflow/workspace/store.py::PgWorkspaceStore.list_recent` = **全局** `"SELECT * FROM workspace_runs ORDER BY created_at DESC LIMIT $1"`（**无 tenant 过滤**）。
  - 测试：`tests/integration/test_inc33_history_rehydrate.py::_record` 把 `created_at` **写死为 `"2026-09-30T00:00:00+00:00"`**。
  - 库：直连 `postgresql://forgeflow:forgeflow@localhost:5433/forgeflow` 实查 `workspace_runs` = **226→228 行**，其中 **211 行**的 `created_at` 晚于该固定值 ⇒ 测试行在 `ORDER BY created_at DESC LIMIT 200` 中排名 **212** ⇒ 落在窗口外 ⇒ 未被回填 ⇒ 断言失败。
- **为何曾经全绿、如今转红**：全量跑时该用例执行靠前、且当时库内行数尚未击穿 200 阈值；随库增长（多轮全量 + 用例自身**不回删** PG 行），阈值被击穿 ⇒ **全量档转红**。⇒ 该用例是**非 hermetic 的状态相关时间炸弹**，且在运维上会**随库自然增长而自动变红**。
- **归属与处置建议（分两层）**：
  1. **测试层（主因，可立即修）**：用例非 hermetic（写死历史日期 + 依赖全局行数窗口）。建议：让 `_record` 的 `created_at` 取**当前时间**，或在 setup 中以 `pg_purge` 清 `workspace_runs`，或改用该租户的 `list_session_runs` 而非全局 `list_recent`。**（本次按工单约束未改任何代码。）**
  2. **产品层（次因，需架构师/用户裁定）**：`hydrate_run_store` 的重启回填是**全局封顶 200 行、无 tenant 维度**，与 INC32-DESIGN ADR-02「跨重启可查」存在张力——**超过 200 行的历史会静默查不到**。属设计取舍（bounded vs 全量/按租户），非崩溃性 Bug，但口径需明示。

### B. 【低·覆盖缺口】`tests/realstack`（14 个真实栈用例）默认门控关闭
- 机制：`tests/realstack/conftest.py::realstack` 要求 `FORGEFLOW_REAL_STACK=1`，否则 `pytest.skip`，且 skip reason 明确（不静默）。默认 15 个 skip 中 14 个来自此。
- 本次**已显式开启并跑通**：`FORGEFLOW_REAL_STACK=1` + 真实 Ollama + 真实 PG → **14 passed / 0 failed（32.74s）**，覆盖 T1 真实 LLM 运行、T2 PG 持久化+重启、T3 指标/成本、T4 反证降级。⇒ 能力**存在且有真实栈证据**，但**常规 CI/离线档不回归**，属“默认不设防”。

### C. 【低·已知缺口】INC12 GAP-Q5 主动标记
- `tests/integration/test_inc12_trust_loop.py::test_q5_observation_carries_parent_pointer` 以 `pytest.skip` 显式标注 `ToolInvocation has no stable identity (no invocation_id)`。属**已知未实现能力**，非回归失败。

### D. 【提示·非缺陷】e2e 中的“非字面 testid”均为动态构造，无真空断言
- 扫描 `frontend/e2e/*` 引用的 74 个 testid 中，13 个未以字面 `data-testid="..."` 出现在 `frontend/src`（证据 `qa_tmp/audit_testid_absent.txt`）。逐一核验后**全部**属以下两类，**非**真空断言：
  - 动态模板构造：`forgeflow/.../ExecDetailPanel.tsx::conv-exec-${c.id}`、`ResultPanel.tsx::result-tab-${t.id}`、`resultActions.ts::result-ctx-${key}`、`ResourcePicker.tsx::resource-kind-*`（数据表格 `testid` 字段）。
  - 仅出现在**注释**中（用例正文明文声明“不写该 testid 的 count=0，因为 src 已删除、写了就是真空假绿”）：`inc39_agent_completion.spec.ts` 对 `result-ctx-code-rerun` / `result-ctx-region` 的处理。
- 结论：e2e 反而**主动防真空**（用 `toEqual([...])` 逐项集合相等、`toHaveCount(1)` 可证伪计数、`*AbsentWithControl` 阳性对照）。此项**无缺陷**。

### E. 【环境假信号·机制说明】全量 pytest `EXIT=1`
- 全量 stdout 末：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":350,"threshold":50,"scope":"turn",...}`。
- 机理：pytest 会话收尾的 `tmp_path` 垃圾回收一次性删除 350 个文件（>阈值 50），触发**宿主机 safe-delete 守卫**，守卫拒绝并终止进程 → 非零退出。**发生在清理阶段，测试结果已全部落盘 junit**。
- 反证：`pytest tests/integration/test_api.py`（不产生批量 tmp）→ `4 passed`，`EXIT=0`。

---

## 6. 覆盖缺口（静态证据，附口径）

> 口径：以 `tests/**` 中出现的 `forgeflow.<dotted>` 导入与 267 个源模块做包含匹配（脚本产物 `qa_tmp/audit_coverage_map.txt`）。**注意**：这是**静态导入可达性**，可能高估覆盖（间接 import / `__init__` 聚合导入）也可能漏掉字符串动态加载，仅作“缺口候选”信号，非行覆盖率。

- 源模块 267 个；**20 个未被测试直接导入（缺口候选）**，按子包：`workflows:6, api:3, agents:2, evaluation:2, state:2, governance:1, memory:1, observability:1, resilience:1, security:1`：
  - `forgeflow.agents.analyzer` / `forgeflow.agents.base`
  - `forgeflow.api.hub_schemas` / `forgeflow.api.resource_schemas` / `forgeflow.api.schemas`
  - `forgeflow.evaluation.dataset` / `forgeflow.evaluation.runner`
  - `forgeflow.governance.context`
  - `forgeflow.memory.relational_store`
  - `forgeflow.observability.tracing`
  - `forgeflow.resilience.retry`
  - `forgeflow.security.email_allowlist`
  - `forgeflow.state` / `forgeflow.state.workflow_state`
  - `forgeflow.workflows.finance_recon.prompts` / `.stages`；`forgeflow.workflows.sales_ops.prompts` / `.stages`；`forgeflow.workflows.support_ops.prompts` / `.stages`
- **真实栈档未纳常规回归**：`tests/realstack`（14 用例）默认 skip（见 §5-B）。
- **仅 happy path / 负向不对称的候选**：本报告未逐函数做分支覆盖，建议对 `forgeflow.api.schemas`、`forgeflow.resilience.retry`、`forgeflow.observability.tracing` 三处（既有缺口候选、又属关键路径）补 unit 用例。
- **跨后端对称性**：本次证实多数用例自带 `[memory]/[postgres]` 双档参数化（如 INC33/INC12 一致性用例），双档四列零差异，**对称性良好**。

---

## 7. 复现命令清单（一键复核）

```bash
# 0) 解释器
PY="C:/Users/18769/.workbuddy/binaries/python/envs/agentflow/Scripts/python.exe"   # 只在 ForgeFlow-main/ 下跑

# 1) 后端离线档
cd D:/Agentxm/Multi-Agent/ForgeFlow-main
STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock "$PY" -m pytest tests --junitxml=qa_tmp/audit_mem.xml -q

# 2) 后端真实 PG 档（先迁移幂等）
export POSTGRES_SYNC_URL='postgresql+psycopg://forgeflow:forgeflow@localhost:5433/forgeflow'
"$PY" -m alembic upgrade head && "$PY" -m alembic upgrade head && "$PY" -m alembic current
STORAGE_BACKEND=postgres LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock "$PY" -m pytest tests --junitxml=qa_tmp/audit_pg.xml -q

# 3) 前端构建门禁
cd frontend
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build

# 4) 前端 e2e（先重建 dist，再看 .last-run.json）
node node_modules/vite/bin/vite.js preview --port 4173 --strictPort &   # 常驻
node node_modules/@playwright/test/cli.js test
cat test-results/.last-run.json

# 5) 附加：真实栈档（需 Ollama + PG）
cd ..
FORGEFLOW_REAL_STACK=1 LLM_PROVIDER=ollama STORAGE_BACKEND=postgres "$PY" -m pytest tests/realstack --junitxml=qa_tmp/audit_realstack.xml -q
```

---

## 8. 原始证据文件索引（`qa_tmp/`）

| 文件 | 内容 |
|---|---|
| `audit_mem.xml` / `audit_mem.log` | 离线档 junit + stdout（1548/0/0/15） |
| `audit_pg.xml` / `audit_pg.log` | 真实 PG 档 junit + stdout（1548/0/0/15） |
| `audit_realstack.xml` / `audit_realstack.log` | 真实栈档 junit + stdout（14/0/0/0） |
| `audit_alembic.txt` | 两次 upgrade + current + heads |
| `audit_build_tsc.txt` / `audit_build_vite.txt` | 构建门禁退出码与产物 |
| `audit_e2e.txt` | Playwright 全量输出（63 passed） |
| `audit_isolated_integration.txt` | 单跑 integration 复现 INC33 红 |
| `audit_inc33_repro.txt` | INC33 单文件复现（1 failed/1 passed） |
| `audit_safe_delete_proof.txt` | 单文件对照 EXIT=0（safe-delete 反证） |
| `audit_coverage_map.txt` | 源码-测试导入覆盖映射（20 候选缺口） |
| `audit_testid_absent.txt` | e2e 非字面 testid 清单（13，均动态/注释） |
| `docker_ps.txt` | 容器拓扑实况 |
| `audit_preview_server.log` | e2e 常驻预览服务日志 |
| `_lead_full_pg2.xml` | **主理人侧**全量 PG 复跑 junit（1548/1/0/15，红点=INC33） |
| `_lead_inc33_single.xml` | **主理人侧** INC33 单例断言失败原文 |

---

## 9. 一句话结论

后端双档在 **QA 采集时点** 各 **1548 用例、0 失败 0 错误**（四列逐文件 0 差异）——但该「0 失败」是**时点相关的侥幸绿**：主理人全量复跑已得 **failures=1**。附加真实栈 **14/14 通过**；前端构建门禁双绿、e2e **63/63 通过**；迁移**幂等**到 head `016`。**高危红点** `INC33 test_history_survives_restart_and_detail_marked[postgres]` 是**非 hermetic 的时间炸弹**（写死历史时间戳 + 全局 `list_recent(200)` 窗口），**已在全量档引爆**；兼带**产品口径**（重启回填全局封顶 200 条、无 tenant 维度）待架构师/用户裁定。全量 `EXIT=1` 另含宿主机 safe-delete 假信号成分。
