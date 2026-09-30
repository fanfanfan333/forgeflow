# INC27 增量 PRD — 把企业能力接入代码执行面（Skill / Memory / RBAC） + 本机 qwen3:8b 真实 E2E

> 文档类型：**简单** PRD（不做竞品 / 市场分析）
> 上游输入：主理人（team-lead）前置侦察结论；`docs/sop/INC26-PRD.md`（承接口径：编号 / 状态词表 / 验收风格）
> 工程基线：`ForgeFlow-main`（FastAPI `forgeflow/` + React 手写 CSS `frontend/src/`）
> 运行档位：A 档离线（`storage_backend=memory` + `llm_provider=mock`）/ B 档真实（PG5433 + 本机 Ollama `qwen3:8b`）
> 本文件位于 `docs/`（本仓不提交红线区）；**不要 git add**。
> 引文纪律：一律 `文件名::符号名` 锚定；**严禁** `file.py:行号`。

---

## 0. 项目信息

- **Language**：简体中文
- **Programming Language**：Python（FastAPI，后端既有栈）+ TypeScript/React 手写 CSS（前端既有栈，**不引入** MUI / Tailwind）
- **Project Name**：`inc27_codeplane_enterprise_binding`
- **原始需求复述**：当前 ForgeFlow 的代码执行面（`SubprocessOpenHandsEngine` + 子进程 runner）**完全没消费企业的技能资产与长期记忆**，也**没有代码专属的 RBAC 门禁**，且**从未在真实模型上端到端验收过**。INC27 要把「技能路由 → 代码智能体上下文」「记忆分层 → 检索上下文」「代码面 RBAC」「本机 `qwen3:8b` 真实 E2E」四件事落到代码执行面上，并证明注入是**承重**的（去掉注入断言必红）。

---

## 1. 产品目标

### 1.1 一句话目标

让 **ForgeFlow（企业控制面）** 真正**管**起 **OpenHands（代码执行面）**：代码任务派发前按 intent 选技能、按需检索长期记忆并注入智能体上下文，用**代码专属 RBAC**门禁「运行代码任务 / 批准代码变更」，并在本机 `qwen3:8b` 上完成一次**带注入**的真实端到端验收——真改对代码、真跑测试、产出真 diff 与真测试报告。

### 1.2 具体目标（正交）

| 编号 | 目标 | 可测口径 |
|---|---|---|
| **G1 技能承重** | 代码任务能消费企业技能资产，且注入**确实影响**系统提示词 | run 记录可追溯被注入技能的 id+版本+名称；反向漂移实验证明去掉注入断言必红（AC-1~AC-4 / AC-20） |
| **G2 记忆承重** | 代码任务能检索并注入长期记忆，可追溯、不臆造 | run 记录可追溯注入了哪些记忆；检索不到即空列表（AC-5~AC-7 / AC-20） |
| **G3 权限闸门** | 代码面「运行 / 批准」是**默认拒绝**的专属权限，越权 403 且**不产生**副作用 | 越权请求不产生 run / 不产生 resume / 不改 approval（AC-9~AC-13） |
| **G4 真实可验收** | 本机 `qwen3:8b` 上带注入跑通一次代码任务并**机械判定**成功 | `degraded is None` ∧ `measured` ∧ `verdict=="passed"` ∧ `exit_code==0` ∧ diff 命中目标文件（AC-14~AC-17） |
| **G5 可见与不回归** | 注入内容对用户可见、可溯源，原始 trace 仍默认折叠 | 前端小节逐字来自 run 记录；既有 testid（`code-trace` 等）不删不改（AC-18~AC-19） |

---

## 2. 背景与现状

> 全部为**已实测**基线（主理人侦察，本节如实压缩，不复制粘贴全文）。未核实的项一律不写。

### 2.1 已经做好（INC25/INC26 交付，**勿重复立项**）

- **事件适配**：`forgeflow/codeplane/events.py` 把 OpenHands/runner 事件白名单适配为 `CodeEvent`；诚实规则（原文只进 `detail` 折叠区、`latency_ms` 未测量为 `None`）。
- **工作区隔离**：`forgeflow/codeplane/workspace.py::WorkspaceManager`（`create/reuse/release`，工作区根在项目树之外）。
- **跨事件循环引擎**：`forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine`（`subprocess.Popen` + `asyncio.to_thread`）。
- **独立 venv runner**：`forgeflow/codeplane/runner/run_code_task.py`（job 走 stdin、事件走 stdout JSONL）。
- **人工审批闭环**：`forgeflow/codeplane/approval.py` + `forgeflow/api/routers/codeplane.py::approve_code_run` / `::reject_code_run`（批准后新建 resume run 携带 `code_prior`）。
- **前端时间线**：`frontend/src/views/runs/CodeTaskTimeline.tsx::CodeTaskTimeline`（**步骤列表 / stepper 形态**，原始 trace 走默认折叠的 `<details data-testid="code-trace">`）+ `frontend/src/views/runs/CodeApproval.tsx::CodeApproval`（diff / 测试 / 三按钮 `code-approve` `code-reject` `code-reanalyze`），二者挂在 `frontend/src/views/runs/ResultPanel.tsx` 的 `code-plane` 区块内。
- **分析链路已有技能注入先例**：`forgeflow/runtime/orchestrator.py::run_task` 会把上下文构造器选中的技能写入 `ctx.available_skills`，并落 run 记录 `skills_used`；技能选择器 `forgeflow/skills/registry.py::SkillRegistry.select(tenant_id, intent, k)` 为**关键字匹配**（无 embedding，离线确定性），返回 published `SkillRecord`（`forgeflow/skills/models.py::SkillRecord`，含 `name` / `current_version` / `domain` / `description`）。
- **记忆接口就绪**：`forgeflow/memory/memory_manager.py::MemoryManager` 有 `remember` / `recall(query, k, namespace, min_similarity)` / `forget` / `format_context`。

### 2.2 核心缺口（INC27 要解决的，**已实测确认**）

1. **代码执行面完全不消费 Skill / Memory**：`forgeflow/codeplane/engine.py` 中 `skill` / `memory` / `context` **零命中**；`forgeflow/runtime/orchestrator.py::_codeplane_args` 只透传 `approval` / `workspace_id` / `prior` / `resource_ids`，**不传任何技能或记忆上下文**；runner 的系统提示词是模块级写死的 `run_code_task.py::_SYSTEM_PROMPT`。⇒ 代码任务与企业的技能资产、长期记忆**断开**。
2. **代码面没有专属 RBAC**：`forgeflow/rbac/policies.py::ROUTE_PERMISSION_MAP` 中与代码面相关的映射只有 `("POST", "/codeplane") -> ("approve", "skills")`；`policies.py::ROLE_PERMISSIONS` 里没有任何「运行代码任务」「批准代码变更」的代码专属权限，也没有按仓库/分支作用域的门禁。
3. **从未在真实模型上端到端验收**：现有测试全走 mock / deterministic，**从未**在真实 `qwen3:8b` 上跑过带 skill/memory 注入的代码任务。

### 2.3 本机实测约束（必须写进验收前提）

- 本机 Ollama **只有 `qwen3:8b` 可用**（`qwen2.5vl:3b` 无 `tools` 能力，不可用）。
- **默认配置下 OpenHands 引擎根本不干活**，必须**同时**满足三处（下称「三毒」）：① `reasoning_effort="none"`（否则首轮烧 ~97s 返回空）；② 传**极简自定义 system prompt**（内置提示词会把 8B 带偏成"介绍工具"）；③ 工具面**只留 `terminal` + `file_editor`**（`task_tracker` 有害）。
- **现状核对**：三毒当前**已**在 runner 内编码——`run_code_task.py::_llm_kwargs` 写死 `reasoning_effort`（默认 `"none"`）、`run_code_task.py::_SYSTEM_PROMPT` 是极简提示、`run_code_task.py::_explicit_tools` 只返回 `terminal` + `file_editor`。⇒ INC27 的挑战不是"造三毒"，而是**注入技能/记忆后三毒仍然成立**（AC-15）。

---

## 3. 用户故事

- **US-1（开发者 · 技能生效）** 作为**开发者**，我希望派发代码任务时，ForgeFlow 能按我的任务意图**自动带上企业技能库里的相关技能**（其 prompt / 步骤 / 适用工具），我希望智能体**按技能约定的方式**去改代码，以便团队沉淀的"怎么改这类 bug"能真正作用到执行面，而不是只写在技能库里没人用。
- **US-2（开发者 · 记忆承接）** 作为**开发者**，我希望代码任务开始前系统能**检索并注入**与该仓库/任务相关的**长期记忆**（例如"这个模块的测试要用 `pytest -q`、这个函数不要动并发"），以便智能体不用每次都从零摸索、不重复踩坑。
- **US-3（管理员 · 配技能 / 配权限）** 作为**管理员**，我希望能配置**哪些技能可被代码任务选中**、并**显式授予**「谁可以运行代码任务」「谁可以批准代码变更」，默认不给，以便代码面改动处在企业可控、可审计的范围内。
- **US-4（审计员 · 溯源）** 作为**审计员**，我希望在代码任务详情里一眼看到**本次注入了哪些技能（含版本）与哪些记忆**，并能下钻到原始 trace；同时看到**谁批准了这条代码变更**，以便复现"这条 diff 是在什么上下文下做出来的"。
- **US-5（越权者 · 被挡）** 作为**一个没有代码权限的角色**，当我尝试运行代码任务或批准代码变更时，我希望系统**明确拒绝（403）且不产生任何副作用**（不留 run、不留 resume run、不动工作区），以便企业边界不是纸面承诺。

---

## 4. 需求池

> 约定：优先级 **P0 必须 / P1 应当 / P2 可以**；每条含「做什么」+「验收标准（AC）」+「可验证方式」。AC 一律**可机械判定**（能写成一条会红的测试），禁止"体验良好"类主观词。既有代码处一律 `文件名::符号名` 锚定。

### 4.1 P0 — 技能路由 → 代码智能体上下文（R-S）

#### P0-1 技能选择与注入代码任务 job
- **做什么**：代码任务派发前（`forgeflow/runtime/orchestrator.py::_execution_args` 组装代码步骤 args 处，或 `::_codeplane_args` 旁路），对**代码任务**调用 `forgeflow/skills/registry.py::SkillRegistry.select(tenant_id, intent, k)` 选技能（published、关键字匹配、离线确定性）；把入选技能的 **spec（name / version / description / steps / 适用工具）** 作为新增字段注入派发给 runner 的 job（如 `skill_context`）。runner 在 `run_code_task.py::_run_conversation` 中把技能 spec **编入** `Agent(system_prompt=...)` 的提示词（在既有极简 `_SYSTEM_PROMPT` 基础上**附加**技能段，不替换）。
- **验收标准**：
  - **AC-1**：对一个意图可匹配 published 技能的代码任务，引擎写入 runner stdin 的 job JSON 中**存在**技能 spec 字段，且其技能 id 集合 == `SkillRegistry.select(...)` 在同一 intent/tenant 下的返回集合（可对比）。
  - **AC-2**：runner 实际传给 `Agent(system_prompt=...)` 的文本**包含**被注入技能的 name/steps（机械断言提示词字符串包含技能内容）。
  - **AC-3**：run 记录可追溯被注入技能的 **id + 版本 + 名称** 三项（三字段均可从 run 记录读出）；且与分析链路**同口径**（同一字段名 + 同一"selected published skills"来源）。
  - **AC-4**：intent 匹配不到任何 published 技能时：job **不含**技能段，run 记录 `skills_used` 为**空列表**，代码任务**照常运行**（不因缺技能失败或降级）。
- **可验证方式**：后端单测（fake skill repo 注入已知技能 → 断言 job JSON 与 system prompt 内容）+ run 记录读取断言。

### 4.2 P0 — 记忆分层 → 检索上下文（R-M）

#### P0-2 记忆检索与注入
- **做什么**：代码任务派发前调用 `forgeflow/memory/memory_manager.py::MemoryManager.recall(query, k, namespace, min_similarity)`（query 取任务 intent，namespace 取任务/仓库维度，k/min_similarity 沿用默认）；命中结果经 `::format_context` 形成 `memory_context`，作为新增字段注入 job；runner 把记忆段**编入**系统提示词（与技能段并列，均附加于极简 `_SYSTEM_PROMPT` 之后）。
- **验收标准**：
  - **AC-5**：代码任务派发路径**确实调用了** `MemoryManager.recall`（可对 fake `MemoryManager` 打桩计数断言），命中结果作为 `memory_context` 进入 job，并被编入系统提示词。
  - **AC-6**：run 记录可追溯**本次注入了哪些记忆**（记忆 id 或可定位来源），可机械读取；未注入则不写该块（不写空壳）。
  - **AC-7**：`recall` **正常无命中**（返回空列表）⇒ `memory_context` 为空、系统提示词**不含**记忆段、**不臆造**任何记忆内容，任务照常运行。
- **可验证方式**：后端单测（打桩 `MemoryManager.recall` 返回固定/空两种结果 → 断言 job 与提示词）+ run 记录断言。

### 4.3 P0 — 代码面 RBAC（R-R）

#### P0-3 代码专属权限 + 默认拒绝 + 审计
- **做什么**：在 `forgeflow/rbac/policies.py` 新增代码专属权限（建议 `run:code` = 运行代码任务、`approve:code` = 批准/拒绝代码变更），并让代码面路由/处理器**强制**该权限（默认拒绝，沿用 `policies.py` 的 fail-closed 纪律）。**运行代码任务**：代码任务经 `POST /tasks` / `POST /runs`（现映射 `execute:workflows`）触发，路由前缀无法区分是否代码任务 ⇒ 需在处理器内做**对象级判定**（`forgeflow/runtime/orchestrator.py::_is_code_task` 为真时额外要求 `run:code`），越权返回 **403 且不产生 run**。**批准代码变更**：`forgeflow/api/routers/codeplane.py::approve_code_run` / `::reject_code_run` 要求 `approve:code`，越权 **403 且不产生 resume run、不动工作区**。所有越权尝试写既有审计通道。
- **验收标准**：
  - **AC-9**：无 `run:code` 的角色发起代码任务 ⇒ HTTP **403**，且 run 存储**计数不变**（不产生任何 run）。
  - **AC-10**：无 `approve:code` 的角色调用 `POST /codeplane/runs/{id}/approve` ⇒ HTTP **403**，且原 run 的 `codeplane.approval.status` **不变**、**不产生** resume run、工作区**未被提交**。
  - **AC-11**：每次越权尝试**写入审计**（既有 audit 通道），条目含 user_id / role / action / 结果。
  - **AC-12**：有权限的角色（如 `admin`）正常路径**不回归**：可运行代码任务、可批准并产生 resume run（既有 INC25 端到端语义不变）。
  - **AC-13**：**默认拒绝**：任何未在 `policies.py::ROUTE_PERMISSION_MAP` 声明的代码面路由请求 ⇒ 拒绝（fail-closed 不回退为放行）。
- **可验证方式**：API 级越权测试（不同 role 打同一路由，断 403 + 副作用计数）+ 审计条目读取 + 既有有权限路径回归。

### 4.4 P0 — 本机 qwen3:8b 真实 E2E（R-E）

#### P0-4 带注入的真实端到端代码任务
- **做什么**：在 B 档（PG5433 + 本机 Ollama `qwen3:8b`）下，构造一个**确定性代码夹具**（小型 Python 包 + 一个种子缺陷 + 一个在当前代码上**必然失败**的测试）并以 `local_path`/`git_repo` 资源登记；为该任务**预置**至少一条能匹配的技能与至少一条长期记忆，使 P0-1 / P0-2 的注入**真实发生**；运行代码任务走完整链路「隔离工作区 → 引擎 → Diff → 测试 → 待审批 → 批准 → artifact」，验收集由机器判定「改对了」。
- **验收标准**：
  - **AC-14**：单次机械通过 = 同时满足 `codeplane.degraded is None` ∧ `codeplane.tests.measured == true` ∧ `tests.verdict == "passed"` ∧ `tests.exit_code == 0` ∧ `codeplane.diff` 非空且**命中目标文件** ∧ 夹具源仓库工作树**不变**。
  - **AC-15**：**三毒在注入上下文之后仍成立**——传给 runner 的 job / `Agent` 构造满足：`reasoning_effort == "none"`；系统提示词仍为极简工程提示（**不含**"介绍工具 / 回答问题"类引导），技能/记忆段为**附加**而非替换；工具面**恰为** `terminal` + `file_editor`（**不含** `task_tracker`）。三者机械检查。
  - **AC-16**：**复现门槛**：连续 **3 次**独立运行中 **≥2 次**机械通过（`qwen3:8b` 非确定性，`temperature=0.0` 仍可能抖动）；`<2` 即**判定失败**，如实记录，**不得**上调阈值迁就；每次留存**原始 pytest 输出**与 diff。
  - **AC-17**：批准后 resume run 的 `artifacts` 至少各含一条 `code_diff` 与 `code_test_report`（真产物，非占位）。
- **可验证方式**：真实档 E2E 跑批（3 次）+ 逐次证据留存；job / system prompt / tools 的机械字段断言。

### 4.5 P1 — 应当具备

| 编号 | 需求 | 优先级 | 验收标准（AC） | 可验证方式 |
|---|---|---|---|---|
| **P1-1** | 前端可见「本次注入的技能 / 记忆」 | P1 | **AC-18**：代码任务详情页出现「本次注入的技能」「本次注入的记忆」小节，内容**逐字来自** run 记录（后端为空 ⇒ 诚实空态，**不编造**）；**AC-19**：原始 trace 仍走默认折叠的 `<details data-testid="code-trace">`（无 `open`），步骤时间线（stepper）形态不变，既有 `data-testid`（`code-plane` / `code-timeline` / `code-trace` / `code-diff` / `code-tests` / `code-approve` / `code-reject` / `code-reanalyze`）**不删不改** | 前端 e2e（playwright）或临时 harness 断言 DOM 文本 == run 记录字段；折叠态断言 |
| **P1-2** | 反向漂移验收（证明注入**承重**） | P1 | **AC-20**：在**真实注入**路径下（非仅 mock），**移除**技能注入 ⇒ AC-2 / AC-3 相关断言**变红**；**移除**记忆注入 ⇒ AC-5 / AC-6 相关断言**变红**。两项均在真实注入下验证，不许只靠推断 | 反向对照实验：置空注入源后跑同一组断言，断言必须失败 |

### 4.6 P2 — 可以具备

| 编号 | 需求 | 优先级 | 验收标准（AC） | 可验证方式 |
|---|---|---|---|---|
| **P2-1** | 前端可展开查看技能 spec（steps / 适用工具） | P2 | 技能小节可下钻到 spec 明细，数据逐字来自 run 记录 | 前端断言 |
| **P2-2** | 记忆注入相似度阈值 / k 可配置 | P2 | 调整配置后注入的记忆条数随之变化（可观测） | 配置驱动测试 |
| **P2-3** | 代码面权限细化为仓库 / 分支作用域 | P2 | 无某 repo 权限的请求对该 repo 的代码任务 403，对其他 repo 不受影响 | API 级作用域测试 |

---

## 5. UI 设计稿（代码任务详情页 · ASCII 线框）

> 视觉约束：沿用既有手写 CSS + oklch token（`frontend/src/styles/tokens.css`）；面板沿用 `panel / panel-head / panel-body`、按钮沿用 `.btn.primary|danger|ghost`、注意条沿用 `.af-note[.warn]`、状态徽章沿用既有 tone。**禁止** MUI / Tailwind。以下层级与**现有** `frontend/src/views/runs/ResultPanel.tsx::ResultPanel` 的 `code-plane` 区块**一致**：`CodeTaskTimeline`（stepper + 默认折叠 trace）在上，`CodeApproval`（diff / 测试 / 三按钮）在下；INC27 **新增**一节「本次注入」位于两者之间。

```
┌─ 运行详情 · 代码任务（真实档 · qwen3:8b）──────────────────────────────┐
│  状态徽章：待审批   引擎：可用 · 未降级                                  │
│                                                                        │
│  ╔═ code-plane ═══════════════════════════════════════════════════════╗ │
│  ║ ── 任务时间线（code-timeline · stepper）────────────────────────── ║ │
│  ║  ● 隔离工作区已就绪                                       12 ms     ║ │
│  ║  ● 代码执行引擎已就绪                                      —        ║ │
│  ║  ● 生成修改方案                                           ✓ 340 ms  ║ │
│  ║  ● 正在执行（编辑 calc.py）                               830 ms     ║ │
│  ║  ● 已生成代码变更                                         —        ║ │
│  ║  ● 测试已执行                                             1.4s      ║ │
│  ║  ● 需要你的决定                                            —        ║ │
│  ║  ▸ 查看原始执行轨迹（原文）        ← code-trace，默认【折叠】       ║ │
│  ║                                                                     ║ │
│  ║ ── 本次注入（code-injected · INC27 新增）───────────────────────── ║ │
│  ║  技能：  · 修复失败测试  v1.2.0   （已注入智能体提示词）            ║ │
│  ║  记忆：  · mem-8f3a「本模块测试用 pytest -q」                       ║ │
│  ║   （后端为空 ⇒ 显示「本次未注入技能 / 记忆」诚实空态，不编造）       ║ │
│  ║                                                                     ║ │
│  ║ ── 代码变更审批（code-approval）────────────────────────────────── ║ │
│  ║  code-diff                                                        ▸ ║ │
│  ║    calc.py                                             +2 / −1      ║ │
│  ║      - return a - b                                                 ║ │
│  ║      + return a + b                                                 ║ │
│  ║  code-tests                                                         ║ │
│  ║    测试结论：通过 · 5 通过 / 0 未通过 / 0 错误                      ║ │
│  ║    测试命令：python -m pytest -q                                    ║ │
│  ║  [ 批准修改 ]（code-approve） [ 拒绝 ]（code-reject）               ║ │
│  ║  [ 重新分析 ]（code-reanalyze）                                     ║ │
│  ╚═════════════════════════════════════════════════════════════════════╝ │
└────────────────────────────────────────────────────────────────────────┘
```

**无代码权限用户（越权 403）**：

```
┌────────────────────────────────────────────────────────────────────────┐
│ ⚠ 你没有运行代码任务的权限（run:code），本次未创建任何运行。             │
│   该尝试已记录到审计。                                                  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 6. 待确认问题（含默认假设）

| 编号 | 问题 | 我的默认假设（未澄清前按此执行） |
|---|---|---|
| **Q1** | `skills_used` 要「含 id + 版本 + 名称」且「与分析链路同口径」——现有分析链路的 `skills_used` 是**纯 id 字符串列表**。是**升级两链路字段形状**（可能触碰既有断言），还是**新增代码面专属块**？ | 优先**统一升级**为含 `{id, version, name}` 的结构以保持同口径，并**保留 id**；若升级会破坏既有分析链路断言，则退回「`skills_used` 保持旧口径 + 新增 `codeplane.injected.skills`」双写方案。请主理人/架构师拍板。 |
| **Q2** | **记忆检索失败**（如 A 档无 PG vector 存储 `MemoryManager.recall` 抛错）时：**降级**（空记忆继续跑）还是**拒绝**任务？ | **降级**为空记忆并继续，同时在 run 记录显式声明 `memory.degraded`（诚实声明，不静默）。理由：与分析链路"context 是增强不是门禁"口径一致。**注意**：这不同于 AC-7 的"正常无命中"，后者一律空列表。 |
| **Q3** | 代码面权限的**粒度与命名**：新增 `run:code` / `approve:code` 两个权限，还是复用既有 `execute:workflows` + `approve:skills`？ | **新增** `run:code` 与 `approve:code`；默认仅 `admin` 持有（`manager` 是否持有请拍板），`sales_rep` / `viewer` / `service` **不持有**（默认拒绝）。命名若架构师有更贴既有 `action:resource` 的写法，以架构师为准。 |
| **Q4** | 「运行代码任务」的**强制点**：代码任务经 `POST /tasks` / `POST /runs`（现映射 `execute:workflows`）触发，**路由前缀无法区分**是否代码任务。 | 在**处理器内**做对象级判定（`forgeflow/runtime/orchestrator.py::_is_code_task` 为真时额外要求 `run:code`），越权 403 且**不产生 run**——与 `policies.py` 注释里 `approve:skills` 在 handler 内强制的既有做法同款。 |
| **Q5** | 记忆 `recall` 的 **namespace / query / k / min_similarity** 口径。 | `query = task.intent`；`namespace` 取任务/仓库维度（具体键由架构师定）；`k` / `min_similarity` 沿用 `MemoryManager` 默认（5 / 0.35）。若架构师给出更贴合"仓库维度记忆"的命名空间约定，以架构师为准。 |

---

## 7. 范围外（Out of Scope · 本次明确不做）

1. **不改 OpenHands 本体**：不 fork、不 vendor、不 merge 两个 venv（`agentflow` ↔ `openhands` 绝不可合并）。
2. **不改造成 Agent Server 的 HTTP / WebSocket 模式**：当前为 subprocess runner（`forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine`）。**这是一处与架构建议文档的显式偏离**（文档建议未来走 Agent Server），本次**显式声明为技术债**并记录理由（Windows 双事件循环下 `Popen` + `asyncio.to_thread` 行为一致、启动期可用性检查与树级取消更易实现），**不在本次重构**。
3. **不引入 embedding / 向量检索**：技能选择沿用 `SkillRegistry.select` 的关键字匹配（离线确定性优先）；记忆不新建向量能力。
4. **不做 PR 自动创建 / 自动合并**：批准后仍只 commit 到任务工作区。
5. **不改四层数据契约（L1 plan / L2 tool_invocations / L3 observations / L4 artifacts）与既有 API 语义**：只在既有口径上**增量扩展**。
6. **不引入 MUI / Tailwind，不新建 vitest**：继续手写 CSS + oklch token；前端验证走 `frontend/e2e/`（playwright）或临时 harness。
7. **不做非代码任务的 OpenHands 化**：分析 / 知识任务编排仍走既有 Orchestrator + 工具链。
8. **不做代码面权限的"漂移告警 / 自动回收"**（如临时提权到期）——仅做静态权限门禁。
9. **不新增数据库表**（除架构师评估确有必要的索引/枚举调整外）。

---

## 附：本增量统计

- **需求条目**：P0 4 条（R-S / R-M / R-R / R-E）+ P1 2 条 + P2 3 条，共 **9** 条。
- **验收标准**：AC-1 ~ AC-20，共 **20** 条。
- **待确认问题**：Q1 ~ Q5，共 **5** 条（每条含默认假设）。
```
