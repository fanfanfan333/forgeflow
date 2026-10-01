/**
 * Shared types for the /tasks (智能任务) page.
 *
 * INC32 / T04 — the fixed demo run and its `DemoRun` type were removed (P0-3):
 * the page now renders only the run's REAL detail (see `realRun.ts`). These
 * types keep the view a pure renderer — no data shape is invented inside a
 * component.
 */
import type { RunSummary } from '../../api/client'

/** Detail density: business-facing (default) vs developer-facing. */
export type ViewMode = 'concise' | 'debug'

/** Lifecycle of a single execution stage. */
export type RunStageStatus =
  | 'pending' // 待执行
  | 'running' // 进行中
  | 'done' // 已完成
  | 'paused' // 已暂停（等待人工审核）
  // Real runs can genuinely fail. The runtime's six-state step contract
  // (INC15) maps onto these stages:
  //   error / unavailable / refused → `failed`
  //   blocked                       → `blocked` (needed, missing input; NOT a failure)
  //   not_applicable                → `na` (the step did not apply to this task)
  // A legacy `skipped` payload is read through the `skipped → blocked` alias.
  | 'failed' // 失败
  | 'blocked' // 受阻（缺输入，非失败）
  | 'na' // 未适用（与任务无关）

/** Where a stage's information came from — a business-readable source. */
export type RunSourceKind = 'web' | 'doc' | 'crm' | 'memory'

export type RunSourceRef = {
  kind: RunSourceKind
  /** Business phrase, e.g. 「网络搜索：Stripe Series E 2026」. */
  label: string
  /** Optional annotation, e.g. 「stripe.com/about · 9,841 字符」. */
  detail?: string
}

/** A memory entry recalled during a stage. */
export type RunMemoryItem = {
  id: string
  namespace: string
  kind: string
  date: string
  /** Cosine similarity, e.g. 0.89. */
  similarity: number
  snippet: string
}

/** A tool invocation made during a stage. */
export type RunToolCall = {
  name: string
  /** 简洁模式下显示的业务语，如「网络搜索」。 */
  bizLabel: string
  agent: string
  ok: boolean
  /**
   * INC15 — the real per-tool duration in milliseconds, or `null` when it was
   * never measured. `null` renders 「—」 (never a fabricated `0`); a sub-ms value
   * keeps its precision (e.g. `0.062`).
   */
  ms: number | null
  /** 原始工程备注（模型/token/成本）。仅调试模式显示。 */
  note?: string
}

/** Approval payload hanging off the human-review stage. */
export type RunApproval = {
  approvalToken: string
  assignee: string
  title: string
  score: number
  icpFit: 'strong' | 'medium' | 'weak'
  riskCount: number
  /** Diff preview lines (keep the leading `+` / `-` semantics). */
  preview: string[]
}

/** Debug-only numbers for a stage. */
export type RunRaw = {
  model?: string
  tokens?: number
  costCny?: number
  costCnyRaw?: number
  checkpoint?: number
}

/**
 * INC17 — the ReAct round's own detail for one stage (from `detail.llm.rounds`).
 * Non-four-layer data rendered **inside the existing card**: the model-supplied
 * args, a bounded result snippet and the model's own text. Absent on pre-INC17
 * runs, so the card simply renders as before.
 */
export type RunRoundDetail = {
  /** The args the model supplied for this call (shown key/value). */
  args: Record<string, unknown>
  /** ≤200-char snippet of the recorded (PI-sanitised) payload. */
  resultSnippet: string
  /** The model's text on the round that issued the call. */
  modelText: string
}

/** One execution stage = one collapsible card. */
export type RunStage = {
  /** Stable, globally-unique id (used for aria-controls; never an array index). */
  id: string
  order: number
  /** Chinese business stage name: 研究 / 分析 / 起草 / 人工审核 / 发送. */
  name: string
  status: RunStageStatus
  /** One-line outcome — the only result text visible while collapsed. */
  summary: string
  agent: string
  agentInitials: string
  agentTone: 'blue' | 'purple' | 'emerald' | 'amber' | 'muted'
  /**
   * INC15 — the stage's real duration in milliseconds, or `null` when it was
   * never measured. `null` renders 「—」 (never a fabricated `0`).
   */
  durationMs: number | null
  sources: RunSourceRef[]
  memories: RunMemoryItem[]
  tools: RunToolCall[]
  /** INC17 — the ReAct round detail (args / result / model text). Optional. */
  round?: RunRoundDetail
  approval?: RunApproval
  raw?: RunRaw
}

/* ------------------------------------------------------------------------- *
 * INC14 — 结果层 (result-first) types.
 *
 * The result layer renders the run's REAL deliverable + a few honest summary
 * lines. Nothing here is invented: `RunConclusion` is simply one line of
 * backend truth (a step's non-empty `note`), and `RunResultState` is the
 * three-state contract the layer renders (骨架 / 错误 / 数据) so the view never
 * shows a stale body while loading or after a failure.
 * ------------------------------------------------------------------------- */

/** One 关键结论 — a non-empty `steps[].note`, verbatim. */
export type RunConclusion = string

/** The result layer's render state. */
export type RunResultState = 'loading' | 'error' | 'data'

/* ------------------------------------------------------------------------- *
 * INC18 — 第三层「数据依据 / 证据」。
 *
 * 一条证据 = 一条 L2 执行记录（`tool_invocations[i]`）的**投影**：它直接派生自
 * 后端唯一事实源，因此条数与 L2 **严格 1:1**，内容可逐字对齐。本文件不新增任何
 * 「推测出来的来源」——没有记录就是没有证据。
 * ------------------------------------------------------------------------- */

/** One piece of evidence backing the deliverable. */
export type RunEvidence = {
  /** Stable key — the invocation's `step_id`, else its index. */
  id: string
  /** Raw tool id, verbatim (never translated into an invented label). */
  tool: string
  /**
   * INC20 / P1-1 — 业务主标识（角色展示名 / 真实工具 id，见 `roles.roleForTool`），
   * 与工作链节点一致，供证据 Tab 的可读主标识使用（`tool` 仍在，作为 title 提示）。
   */
  bizLabel: string
  /** Raw backend status, verbatim. */
  status: string
  /** Chinese label for `status` (受阻 / 未适用 …), for the business-facing mode. */
  statusLabel: string
  /** Whether the handler really ran — only then is there anything to cite. */
  executed: boolean
  /** Real measured ms, or `null` when never measured (renders 「—」, never 0). */
  ms: number | null
  /** The recorded return, verbatim and bounded (already PI-sanitised upstream). */
  snippet: string
  /** Evidence fingerprint (`result_ref`) — the artifact ⇄ evidence join key. */
  resultRef: string | null
  /**
   * True when the call was served by a development stub (no real upstream).
   * Its payload is real *as a recorded return* but it is NOT real-world data,
   * so the UI must label it — showing it unlabelled would let a mock URL read
   * as a source.
   */
  stub: boolean
}

/* ------------------------------------------------------------------------- *
 * INC18-B — 第二层「结构化结果」。
 *
 * 全部**派生自 L4 产物正文本身**（`artifacts[i].content`）：按 Markdown 标题切
 * 章节、按列表项取关键发现、按表格取指标。规则只有一条——**正文里没有的，界面上
 * 就不出现**。因此本机（无真实业务数据源）不会产生任何指标卡，而不是编一张。
 * ------------------------------------------------------------------------- */

/** One Markdown heading + its body, verbatim. */
export type RunSection = {
  id: string
  /** Heading level (1..6) — used for nesting, never for styling hacks. */
  level: number
  title: string
  body: string
  /**
   * INC19 — true when the section is the platform's own **engineering ledger**
   * (执行记录 / 任务计划 / 未适用 / 受阻 / 失败). Those are Trace, not
   * deliverable: they belong behind 「查看完整报告」/ 执行轨迹, never on the first
   * screen. Classified by the real heading text — not by guessing intent.
   */
  engineering: boolean
}

/**
 * INC19 — the four tabs of the run page. Each piece of information has exactly
 * ONE home; a tab switch never duplicates content, it only changes which home
 * is on screen.
 */
export type RunTab = 'result' | 'evidence' | 'trace' | 'cost'

/** One real row of the artifact's first Markdown table (label / value / …). */
export type RunMetric = {
  label: string
  value: string
  /** Remaining columns, verbatim (e.g. 状态 / 摘要). */
  rest: string[]
}

/**
 * INC18-B — 交付状态六态。**只由既有字段派生**，不新增后端状态值。
 * 判定优先级见 `realRun.deliveryState`。
 */
export type DeliveryState =
  | 'waiting' // 未终态：还在跑
  | 'need_approval' // 有步骤停在待审批
  | 'failed' // 有错误 / outcome=failure
  | 'aborted' // 已中止（INC32 起可达：`POST /runs/{id}/abort` 的终态）
  | 'interrupted' // 已中断（INC33：后端终态词表 `TERMINAL_STATUSES` 之一）
  | 'rejected' // 已拒绝（INC33：同上，被拒绝而非崩溃）
  | 'blocked' // 全部应执行的步骤都受阻，且无产物
  | 'partial' // 完成但有步骤未执行
  | 'completed' // 完成且该跑的都跑了

/* ------------------------------------------------------------------------- *
 * INC20 / T05 —— 「成本与记忆」Tab 的诚实化类型。
 *
 * 「未测量 ≠ 0」：是否渲染 Token/成本**不再**由 `typeof === 'number'` 判定，
 * 而由**模型驱动证据**（`roles.isModelDriven`）判定——判定结果在此显式化，避免
 * 判定散落在 JSX 里。
 * ------------------------------------------------------------------------- */

/**
 * 一条成本事实（label + value）。`raw === true` 表示该行是**原始工程值**，
 * 仅在 **debug 密度**下渲染（简洁密度只呈现业务表述，见 `roles.runtimeModeLabel`）。
 */
export type CostFact = {
  label: string
  value: string
  raw?: boolean
}

/**
 * 「本次运行是否调用过模型」的显式判定结果。
 *
 * * `modelDriven === true` ⇒ Token/成本为**已测量值**（可能为 0，此时 0 是真实计量）；
 * * `modelDriven === false` ⇒ **不**渲染 Token/成本，改为诚实说明 `note`（非事实行）。
 */
export type RunCostEvidence = {
  modelDriven: boolean
  /** 未调用模型时的诚实说明；模型驱动时为 `null`。 */
  note: string | null
}

/**
 * 经验条目（展示用的**只读投影**）——`Experience`（`client.ts`）的精简子集。
 *
 * 只取成本/记忆 Tab 真正要渲染的字段（`summary` / `outcome` / `tags` / `memory_ids`
 * / `created_at`），**不带** `decisions` / `reusable_steps` 等噪音。`memory_ids` 为
 * **id 级**记忆列表（内容级需后端按 run 过滤，本期不做）。
 */
export type RunExperienceItem = {
  id: string
  summary: string
  outcome: string
  tags: string[]
  memoryIds: string[]
  createdAt: string
}

/* ------------------------------------------------------------------------- *
 * INC39 — 结果层段⑤「上下文快捷操作」的类型（纯类型，无运行时逻辑）。
 *
 * 取代 INC24 的「恒三档」动作（`NextActionKind` / `NextAction` / `NextActionsDerivation`
 * 已随之退役）：动作**不再固定**，而是按当前任务**真实产物**动态派生 **0～3** 条
 * （派生规则见 `resultActions.deriveContextualActions`）。目标是让用户感觉在**与 Agent
 * 对话**，而不是操作任务审批面板：不再有「让智能体处理 / 我自己处理 / 查看变更」三档固定按钮，
 * 也不再把降级文案伪装成 Agent 结论。
 * ------------------------------------------------------------------------- */

/**
 * 一条上下文快捷操作的**执行机制**（决定点击时真调哪条通路；每条都必须有可观测的真实效果）：
 *   * `continue` —— 真调 `POST /workspace/tasks`（带 `parent_run_id`），提交一条业务指令。
 *   * `diff`     —— 滚动到真实代码变更（既有 `#code-diff`）—— **仅当确有变更文本**。
 *   * `export`   —— 纯前端下载完整原文（`useArtifactEdit.exportResult`，含可见确认）。
 *   * `sources`  —— 切到「证据」Tab（`onTabChange('evidence')`，真实可见态变化）。
 *   * `trace`    —— 切到「执行轨迹」Tab（`onTabChange('trace')`，真实可见态变化）。
 *
 * ⚠️ **无 `rerun` 档（INC39 收敛，勿加回）**：段⑤ 只提供别处没有的入口；「重新运行」由
 * 段④ `result-rerun` / 段① `result-env-rerun` 专责（同守卫、同回调），段⑤ 再加即重复。
 */
export type ContextActionKind = 'continue' | 'diff' | 'export' | 'sources' | 'trace'

/** 段⑤ 的一条上下文快捷操作。 */
export type ContextAction = {
  /** 稳定、英文小写连字符 key（用于 React key + 拼接 testid）。 */
  key: string
  /** 业务可见标签（**不得**含工程术语）。 */
  label: string
  kind: ContextActionKind
  /** 对应 testid：`result-ctx-<key>`。 */
  testid: string
  /** 仅 `continue` 档：将提交的**业务化**指令文本（无工程词）。 */
  instruction?: string
}

/**
 * 段⑤ 动作派生**只吃已派生的真实业务值**（不摸原始 `RunDetail`）：
 * 计数均为**已渲染列表的 `.length`**。
 *
 * ⚠️ INC39 收敛（勿加回）：本契约**只保留派生真正消费**的字段（原 `hasDeliverable` /
 * `canRerun` / `missingInputs` / `unrunSteps` 已随「去重规则」一并移除）——`canRerun`
 * 删掉后「重新运行」不再由段⑤ 产出，其余三个在派生里本就未被读取；不留未使用字段。
 */
export type ContextActionInput = {
  /** 代码执行面（`codeplane.present` + `codeplane.diff.present`）。 */
  codeplane: { present: boolean; diff: { present: boolean } }
  /** 指标条数（`metrics.length`）。 */
  metrics: number
  /** 关键发现条数（`findings.length`）。 */
  findings: number
  /** 来源条数（`sources.length`）。 */
  sources: number
  /** 产物条数（`artifacts.length`）。 */
  artifacts: number
}

/* ------------------------------------------------------------------------- *
 * INC25 / T05 —— 代码执行面（codeplane）的展示类型。
 *
 * 全部**派生自后端真实字段**（`GET /runs/{id}`.codeplane），本文件不新增任何后端
 * 状态值：时间线 `status` 复用既有步骤状态词表（`stepStatusToStageStatus`），
 * 测试 `verdict` 复用 `passed | failed | unmeasured`，绝不引入第二套词汇。
 * ------------------------------------------------------------------------- */

/** 时间线的一项 = 后端 `codeplane.timeline[i]`（一个标准化的引擎事件）。 */
export type CodeTimelineItem = {
  seq: number
  ts: string
  phase: string
  kind: string
  /** 复用既有步骤状态词表（不新增第二套词汇）。 */
  status: string
  /** 原始工具 id —— **绝不**出现在可见行，只在折叠的 Trace 内（AC-13）。 */
  tool: string
  /** 业务文案 —— 可见行**只**显示它。 */
  label: string
  /** 原始 Tool / Action / Observation / model 文本 —— 只在折叠 Trace 内（AC-13）。 */
  detail: string
  /** 实测毫秒；未测量为 `null`（渲染「—」，**绝不** 0，AC-14）。 */
  latencyMs: number | null
}

/** Diff 的一行（保留 `+` / `-` 语义）。 */
export type CodeDiffLine = {
  kind: 'add' | 'rem' | 'ctx'
  /** 行原文，逐字（含前导 `+` / `-` / 空格）。 */
  text: string
}

/** Diff 的单个文件（按文件分组呈现，AC-15）。 */
export type CodeDiffFile = {
  path: string
  additions: number
  deletions: number
  lines: CodeDiffLine[]
}

/** 代码变更（`codeplane.diff` 的解析结果；无变更 ⇒ `present=false` 的诚实空态）。 */
export type CodeDiff = {
  /** 是否存在真实变更文本（无改动 ⇒ false，界面给诚实空态，绝不编造）。 */
  present: boolean
  /** 原始 unified diff 全文，逐字（无法分文件时直接原样呈现它）。 */
  text: string
  files: CodeDiffFile[]
}

/** 测试结论（判定权在 ForgeFlow 侧；不可解析 ⇒ 未测量，**绝不**默认通过）。 */
export type CodeTestResult = {
  /** 是否真的量出了测试结果（`false` ⇒ 未测量）。 */
  measured: boolean
  /** `passed | failed | unmeasured`。 */
  verdict: string
  passed: number
  failed: number
  errors: number
  failedCases: string[]
  /** 真实执行的测试命令，逐字。 */
  command: string
}

/** 代码任务的审批状态（界面**仅**三动作，AC-18）。 */
export type CodeApproval = {
  /** `pending | approved | rejected`（未知值原样保留）。 */
  status: string
  approvalId: string
  decidedBy: string
  decidedAt: string
  /** 是否真的完成了提交（批准后的复跑才为 true）。 */
  committed: boolean
}

/**
 * 本次注入的 Skill / Memory 上下文（后端 `codeplane.injected` 的只读投影）。
 *
 * 空列表 = 本次运行**没有**注入任何东西（诚实空态），组件据此**不渲染**该段 ——
 * **绝不**补占位、**绝不**编造。
 */
export type CodeInjectedContext = {
  /** 选中的 Skill（`id` + `version` + `name`，逐字后端字段）。 */
  skills: { id: string; version: string; name: string }[]
  /** 召回并注入的长期记忆（`id` + `scope`，逐字后端字段）。 */
  memory: { id: string; scope: string }[]
}

/**
 * INC29 T02 (§6) —— 进展摘要词表（后端 `codeplane.summary` 的只读投影）。
 *
 * 每个字段都来自**真实证据**（unified diff / 评审的测试结论）；**未测量一律 `null`**，
 * **绝不**用 `0` 冒充（与 `latencyMs` 同一条诚实纪律）。
 */
export type CodeTaskSummary = {
  /** 变更文件数（来自 diff）；未测量 ⇒ `null`。 */
  filesChanged: number | null
  /** 真实执行的测试命令，逐字；未测量 ⇒ `null`。 */
  testCommand: string | null
  /** 通过的用例数；未测量 ⇒ `null`（不是 0）。 */
  passed: number | null
  /** 未通过的用例数；未测量 ⇒ `null`（不是 0）。 */
  failed: number | null
  /** 自动修复轮次（失败后的测试重跑次数）；无测试执行证据 ⇒ `null`。 */
  repairRounds: number | null
}

/* ------------------------------------------------------------------------- *
 * INC36 —— 会话工作台「ChatGPT 式分层」的 L1/L2 类型（纯类型，无运行时逻辑）。
 *
 * 全部为 **additive**：不新增后端字段、不改既有类型。派生逻辑一律在
 * `conversation.ts`（纯函数），组件只消费派生结果。
 * ------------------------------------------------------------------------- */

/**
 * L2「查看执行详情」的六类结构化字段 id（`Planner` / `Knowledge Search` /
 * `Skill` / `Tool` / `Memory` 五类 + 详细 Trace 入口）。
 */
export type ExecCategoryId = 'planner' | 'knowledge' | 'skill' | 'tool' | 'memory'

/**
 * 一类执行详情（**存在性 + 计数 + 状态**）。`present === false` 表示后端 payload
 * 里**没有**这一类 —— 界面据此**不渲染**该行（诚实：绝不臆造一条假记录）。
 *
 * `count` 为该类**真实计数**（列表长度之和）；`statuses` 为该类涉及调用的
 * **业务状态词**去重列表（无则空数组，界面不渲染状态列）。
 */
export type ExecCategory = {
  id: ExecCategoryId
  /** 展示名（`Planner` / `Knowledge Search` / `Skill` / `Tool` / `Memory`）。 */
  label: string
  /** 后端 payload 里是否**确有**这一类（false ⇒ 不渲染）。 */
  present: boolean
  /** 该类真实计数（0 仅在 `present === false` 时为真）。 */
  count: number
  /** 相关调用的业务状态词（去重、保序；可为空数组）。 */
  statuses: string[]
}

/** 左列按天分组的桶键（今天 / 昨天 / 更早）。 */
export type DayGroupKey = 'today' | 'yesterday' | 'earlier'

/**
 * 左列按**真实 `created_at`** 分组的一个桶（`conversation.groupRunsByDay` 产出）。
 * 只包含非空桶（无 run 的桶不出现）。
 */
export type DayGroup = {
  key: DayGroupKey
  title: string
  runs: RunSummary[]
}

/** 整个代码执行面区块（由 `realRun.deriveCodePlane` 装配，供结果层挂载）。 */
export type CodePlaneView = {
  /** 本次运行是否是代码任务（`codeplane` 非空）。 */
  present: boolean
  /** 降级标识（`engine_unavailable` / `model_unavailable` / … 或 `null`）。 */
  degraded: string | null
  /** 引擎是否可用（`null` ⇒ 后端未记录，不臆断）。 */
  engineAvailable: boolean | null
  /** 引擎不可用时的**逐字**原因（诊断用，只进 `title`）。 */
  engineReason: string
  workspaceId: string
  /** `created | active | released | destroyed`。 */
  workspaceState: string
  timeline: CodeTimelineItem[]
  diff: CodeDiff
  tests: CodeTestResult
  approval: CodeApproval
  /** 进展摘要词表（§6）；后端未提供 ⇒ 各字段 `null`。 */
  summary: CodeTaskSummary
  /** 受影响步骤（后端逐字业务名，供降级说明点名）。 */
  affectedSteps: string[]
  /** 已完成步骤（后端逐字业务名）。 */
  completedSteps: string[]
  /** 本次注入的 Skill / Memory 上下文（空 ⇒ 什么都没注入）。 */
  injected: CodeInjectedContext
}
