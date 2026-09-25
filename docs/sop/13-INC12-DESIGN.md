# INC12 — 执行语义真实性（Execution Truthfulness）设计

> 增量范围：A1（真实工具执行 + 调用留痕）、A3（环境闸门：生产不许静默 mock）。
> 代码根：`ForgeFlow-main/`。本文档只描述本轮实际交付的内容与**诚实边界**。

---

## 1. 现状（file:line 级）与问题定性

`forgeflow/runtime/orchestrator.py` 有**两条**执行路径，**两条都在没有真正调用
工具的情况下把步骤记成 `ok`**：

| 路径 | 位置（改动前） | 行为 |
|---|---|---|
| `_llm_executor` | `orchestrator.py:411` | RBAC 通过 → `PolicyEngine.evaluate_tool_call` 放行 → **直接** `steps.append(plan_step.to_payload(index, status="ok"))`，全程没有任何一次真实工具调用 |
| `_default_executor` | `orchestrator.py:515` | `steps.append({**step, "index": index, "status": "ok"})`，同样没有任何真实调用 |

对照 `orchestrator.py:405-414`（LLM 路径）与 `orchestrator.py:509-520`（确定性路径），
两条路径在 "放行" 与 "记 ok" 之间**没有任何 handler 调用点**。

### 为什么这是"执行语义不真实"，而不是普通功能缺失

```
LLM 说：调用 research.search
  → RBAC 允许 → ABAC 允许 → Policy 允许
  → 系统记录：research.search executed ✅
  → 实际上：research.search 根本没执行
```

平台能证明"这个 Agent **不允许**做什么"（RBAC/ABAC/Policy/HITL 都是真实的），
却**不能证明"被允许之后它确实做了什么、拿回了什么"**。治理层再精细，只要被治理的
执行结果本身是伪造的，前面所有治理层都在**管理一个假象**。

**本轮验收命题**：任何一步只有在**真实 handler 真的被调用并返回了结果**时才可以记
`ok`；否则必须是 `error` / `refused` / `unavailable` / `skipped` 之一，并带上
"为什么没执行"的诚实原因。

---

## 2. 组件

新增三个模块（本模块即"单一执行事实源"）：

```
forgeflow/runtime/tool_handlers.py   工具真实实现层（9 个 handler）
forgeflow/runtime/tool_registry.py   ToolBinding 注册表（tool_id → handler/kind/provider）
forgeflow/runtime/tool_executor.py   ToolExecutor —— 唯一执行入口 + 统一 ToolInvocation 记录
```

数据流：

```
orchestrator._llm_executor / _default_executor
  → ToolExecutor().execute(tool, ctx, policy_decision, approval_id)
      → tool_registry.resolve(tool)            # 未绑定 ⇒ unavailable
      → 环境闸门(dev 才允许 development binding)
      → binding.handler(args, ctx)             # 真实调用（唯一可能记 ok 的唯一来源）
      → ToolInvocation.to_dict()
  → task.context["tool_invocations"] 追加（每轮 replan 都留痕）
  → RunRecord.tool_invocations         →  GET /runs/{id}
```

### 2.1 `ToolInvocation` 字段（与目标架构字段一一对应）

见 `tool_executor.py` 的 `@dataclass ToolInvocation`：`run_id / step_id / agent_id /
tool / arguments_hash / tenant_id / data_scope / policy_decision / approval_id /
status / executed / development_stub / provider / summary / result_ref / latency_ms /
error / started_at / attempt / payload`。

---

## 3. ToolExecutor 的 status 语义表（契约，逐行被测试钉住）

| status | executed | 含义 | 是否算 run 失败 | 代码分支 |
|---|---|---|---|---|
| `ok` | `True` | handler 真跑了并返回结果 | 否 | `execute()` 步骤 5（`raw["ok"] is True`） |
| `error` | `True` | handler 跑了但抛异常 / 返回业务失败 | 是 | 步骤 3（`except`）、步骤 4（`ok=False` 且非 `not_executed`） |
| `unavailable` | `False` | 该工具没有任何实现 | 是 | 步骤 1（`resolve()` 返回 `None`） |
| `refused` | `False` | 有实现但被环境闸门拒绝 | 是 | 步骤 2（`development` binding 且环境非 dev） |
| `skipped` | `False` | 有实现但没有有效输入，未执行 | 否 | 步骤 4（`ok=False` 且 `not_executed=True`） |

`_record_invocation()`（`orchestrator.py`）在 run 层把
`status ∈ {error, unavailable, refused} ⇒ errors.append(...)`；
`skipped` **不进 errors**，但会 emit 一条 `run.warning`（`reason="tool_skipped"`）。

### 3.1 固定执行顺序（顺序即契约，不可调整）

`tool_executor.py::ToolExecutor.execute`：

1. `resolve(tool)` — 解析不到 ⇒ `unavailable`、`executed=False`、`error="未绑定任何实现的工具：<tool>"`。**绝不返回 ok**。
2. 环境闸门（A3）— `get_settings().allows_development_tools()` 为 `False` 且
   `binding.kind == "development"` ⇒ `refused`、`executed=False`、
   `error="环境 <env> 禁止使用开发态工具 '<tool>'（provider=<provider>），拒绝并如实上报"`。**绝不 fallback 到 mock**，**绝不调用 handler**。
3. `time.perf_counter()` 计时 + 调用 `binding.handler(args, ctx)`；**任何异常**都捕获 ⇒ `error`、`executed=True`、`error=str(exc)`（**不吞、不降级成 ok**）。
4. 返回体 `{"ok": False, ...}`：带 `"not_executed": True` ⇒ `skipped`、`executed=False`；否则 ⇒ `error`、`executed=True`。
5. 返回体 `{"ok": True, ...}` ⇒ `ok`、`executed=True`。
6. 计算 `latency_ms`、`arguments_hash = sha256(canonical json(args))`、`result_ref`（优先用 handler 返回的 `result_ref`，否则 `sha256(canonical json(payload))[:32]`）、`summary`（≤200 字）。
7. `payload` **有界**：JSON 形式超过 `MAX_PAYLOAD_CHARS = 4000` 时替换为
   `{"truncated": True, "original_length": N, "preview": <前4000字符>}`；对**外部来源**
   （`_EXTERNAL_TOOLS = {"research.search"}`）的结果先经
   `forgeflow.security.tool_output_guard.sanitize_tool_output(tool, payload)` 净化再落库。
   内部 stdlib 工具不强制包 guard。
8. `data_scope`：从真实来源取值；取不到就 `None`（见 §4）。

---

## 4. 诚实边界（必须逐条说清）

1. **`code.run` 只做校验，不执行任意代码。** `tool_handlers.code_run` 用
   `ast.parse` + `compile()` 做**确定性校验**，**不 `exec` / 不 `eval` / 不起子进程**。
   工具名虽然叫 `run`，平台**不会运行用户代码**。该边界写在 handler 的 docstring 与本节。
2. **`data.query` 是 development stub，平台上没有真实数仓。** 它复用
   `mcp/server/tools/data_tools.query_db`，返回的是**写死的合成行**，恒标注
   `development_stub=True`。复用的 5 个工具中，`research.search` 在未配置 Tavily 时也是
   dev stub（`provider="development-stub"`）。
3. **`data_scope` 为 `None` 的原因。** 平台**没有** DataScope 模块；`PolicyEngine` 的
   `EvalDecision` 也不携带 scope 字段，而 `ToolExecutor.execute` 只收到
   `policy_decision` 字符串。因此没有任何**真实来源**可填充 `data_scope`，本处一律记为
   `None`，**不编造字符串**。这是明确登记的缺口，供后续增量补齐。
4. **`agent_id` 为 `None` 的原因。** `PlanStep` 只带 `tool` / `note` / `step_type`
   （`step_type` 恒为 `"agent"`，不是 agent 身份），没有可归属的真实 agent id，故记 `None`。
5. **`tool_invocations` 是进程内 run 记录。** hub run **不落库、不跨重启**（与 INC9 B4 关于
   `loop=` 的结论一致）。`GET /runs/{id}` 能看到它，是因为它挂在内存 run store 上的
   `RunRecord.tool_invocations`；进程重启后不保留。跨重启的持久化面包屑本轮不做。
6. **默认 profile 的行为变化。** 改动前确定性默认 3 步全记 `ok`；改动后：
   - `research.search` → `ok`（dev 下 development-stub，或配置了 Tavily 时为 real）；
   - `data.query` → `skipped`（plan 步骤没有表名，handler 无有效输入）；
   - `code.run` → `skipped`（plan 步骤没有 paths/repo_path）。
   这是**正确**的：这些步骤在改动前根本没有真实输入，旧行为是伪造的成功。
   `skipped` 不计入 errors，run 仍 `completed`。
7. **可复用的输入只有 intent 与 observation。** `PlanStep` 没有 per-step 参数袋，
   `orchestrator._tool_args()` 提供 `text/intent/query = task.intent` 与
   `observations = 本 run 迄今的 invocations`。因此 `docs.parse` / `policy.check` /
   `research.search` 有真实输入；`data.query` / `code.run` 没有 ⇒ `skipped`。

---

## 5. A3 环境闸门

`forgeflow/config.py`：

* 新增 `app_env: str = Field("dev")`，合法值 `dev | staging | prod`（小写归一化；
  非法值 → 视为 `prod` 并 `logger.warning`，**fail-closed**）。
* `environment() -> str` 返回归一化后的环境名（`dev | staging | prod`）。
* `allows_development_tools() -> bool`：仅 `app_env == "dev"` 为 `True`。
* `is_production()` **复用同一处归一化逻辑**，但**返回值逐字不变**：
  仍等价于 `otel_environment in {"prod", "production", "staging"}`（未知值仍为 `False`）。
* `validate_runtime()` 新增**一条**：非 dev 环境（`environment() != "dev"`）下若 LLM 链
  里仍配置了开发态 stub `mock` ⇒ 记为 fatal。**理由**：`app_env` 是新的工具闸门，
  而既有 T1 检查只看 `otel_environment`；只设 `APP_ENV=prod` 而忘了改
  `OTEL_ENVIRONMENT` 会让 misconfig 漏检。该检查有明确的配置信号（`mock` in provider
  chain），**不是凑数的假检查**；默认 `dev` 下不触发，离线档行为零变化。

`mcp/server/tools/search_tools.py::web_search`：Tavily 未配置时——
`dev` 保持返回 mock（warning 文案升级带 `development-stub` 字样）；
非 dev → **不返回 mock**，返回 `[{"error": "tavily_unconfigured", "query": query}]` 并 `logger.error`。

`mcp/server/tools/data_tools.py::query_db`：**恒为写死假数据**——`dev` 保持现状；
非 dev → **抛 `RuntimeError`（fail loud）**。**二选一理由**：其声明返回类型是
`list[dict]`，返回 `{"error": ...}` 会**静默违反契约**、可能被当作数据迭代；抛异常让
"这是 stub" 无法被误读为真实数据。

**默认 `dev` ⇒ 离线档行为零变化**（所有既有 `validate_runtime` / `is_production` 测试不变红）。

---

## 6. 为什么本轮**没有**做 Validator / Replan 失败重规划子系统

明确不在本轮范围（属独立增量）：

* 本轮的**唯一命题**是"执行语义真实"——把 `ok` 变成"真的执行过"。`validate()`
  （`forgeflow/validation/validator.py`）与 replan 决策（`validation/replan.py`）本身
  没有伪造执行，不需要改；改它们会扩大 diff 并引入与本节无关的行为变化。
* 新增的 `tool_invocations` 已经为未来的 Validator/Replan 提供了**真实证据源**
  （每一步的 status/executed/result_ref），未来增量可以据此重规划，但**本轮不做**。
* `validate()` **未改动**（`git` 可证）。

---

## 7. 回滚方式

本轮为**加性**改动，回滚成本低：

1. 单文件回滚：
   - 删除新增三文件：`forgeflow/runtime/tool_handlers.py`、`tool_registry.py`、`tool_executor.py`；
   - `git checkout -- forgeflow/runtime/orchestrator.py forgeflow/api/hub_schemas.py forgeflow/api/routers/runs.py forgeflow/config.py forgeflow/mcp/server/tools/search_tools.py forgeflow/mcp/server/tools/data_tools.py`；
   - 删除新增测试与本文档。
2. 若只想停用 A3 工具闸门而保留 A1：把 `Settings.app_env` 固定为 `"dev"`（默认即如此），
   则 `allows_development_tools()` 恒 `True`，环境闸门不再拒绝任何 binding。
3. 若只想停用"真实执行"而保留闸门：不建议——那正是本增量要消除的假 `ok`。

数据库、私有依赖、迁移均**无涉及**，无 alembic 变更。

---

## 8. 测试证据（见 `tests/unit/test_inc12_*.py`）

* `test_inc12_tool_executor.py`：逐条钉住 5 个 status；`UNBOUND_TOOLS` 全 `unavailable`；
  非 dev 下 development binding `refused` 且 handler **零调用**；`arguments_hash`
  稳定/可区分；`payload` 有界且标注；外部结果过 guard。
* `test_inc12_llm_executor_real.py`：LLM 路径无输入步骤为 `skipped` 而非 `ok`；
  每步带 `observation`；`tool_invocations` 经 `GET /runs/{id}` 真能看到；
  replan 每一轮都留痕。
* `test_inc12_env_gate.py`：`app_env` 归一化（非法 → prod）与 `allows_development_tools`；
  `is_production` 返回值不变；`validate_runtime` 新检查；`web_search` / `query_db` 的
  非 dev 行为。
