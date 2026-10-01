/**
 * INC14 / INC19 / INC24 — 结果层 (result-first panel) — the first visual layer of /tasks.
 *
 * It answers "我让 Agent 做了什么、结果是什么、依据是什么" from REAL backend data,
 * before any execution detail is shown. Nothing on this panel is invented:
 *
 *   * **INC19 分区**：`result-body` 渲染的是产物正文的**交付部分**
 *     （`partitionArtifactBody(content).deliverable`，逐字、零改写、不重排）；产物
 *     正文自带的**工程账本**（`## 一、执行记录` … `## 五、失败` + 页脚）**不在**结果
 *     Tab，而是在「执行轨迹」Tab 的 `execution-ledger` 内**完整可查**（`<pre>` 原文）。
 *   * 关键结论（`result-conclusions`）取数自**交付部分**的「最终答案」一节（散文行）；
 *     与 `result-findings`（列表项）按行型互斥，二者绝不重复。
 *   * 来源 only ever contains URLs a real (non-stub) tool call returned;
 *   * a run with no deliverable shows the approved honest empty state.
 *
 * ── INC24 —— 「结果」Tab 固定**六段**（自上而下，DOM 顺序即语义顺序，AC-4 可判）─────
 *   ① 任务状态   result-layer-title / result-delivery / [result-status-line] / result-intent
 *   ② 一句话结论 [result-headline]（降级时 [result-headline-fallback]）
 *   ③ 核心发现   result-metrics? / result-findings? / result-conclusions（恒在，渲染 slice(1)）
 *   ④ 当前阻塞   [result-blockers]{ degrade|no-deliverable / missing-inputs / partial / rerun }
 *   ⑤ 下一步动作 [result-next-actions]{ agent / self / view-diff / quick / continue / ctas }
 *   ⑥ 详细证据   [result-details]<details>（初始无 open）{ body / sections? / empty? / footer / 入口 }
 *   常驻（不编号，**穿插于六段之间**）：`res-tabs` 夹在 段① 前半（`result-layer-title` /
 *   `result-delivery`，位于 `.res-head` 内、Tab 条**之上**）与 段① 后半（`result-status-line` /
 *   `result-intent`，位于 `#res-panel-result` tabpanel 内、Tab 条**之下**）之间 —— 即 段① 被
 *   Tab 条劈成前后两半；`result-skeleton` / `res-error` 与 `res-tabs` 同级，出现时取代整块内容。
 *
 * 本文件为**装配器**：段①③ 内联，段②④⑤⑥ 各由独立子组件渲染（高内聚、低耦合）。
 * 段②④⑤⑥ 的诚实降级 / 折叠 / 页脚口径分别写在各自文件顶部。
 *
 * ── P0-2 vs P0-5 边界（勿删）────────────────────────────────────────────────
 * P0-2 优先适用于**产物正文**：正文是后端产品内容，逐字渲染，即使其中恰好出现
 * 工程词（如 `model` / `token`）也**不得**被改写或隐藏——忠实优先于降噪。INC19 的分区
 * 只把**整段**迁到正确的容器（按真实 `##` 标题归属），不改写 / 不摘要 / 不翻译 / 不重排。
 * 「完整产物仍可逐字取得」由两处保证：① 执行轨迹 Tab 的账本原文；② 导出 / 复制 / 打印
 * 用的是**完整原文**（`saved`，含平台执行账本）。
 * P0-5 只约束**我们自己撰写**的界面文案（本文件里所有中文字面量）：它们不得含
 * 工程术语。二者冲突时（正文含工程词）以 P0-2 为准。
 * ─────────────────────────────────────────────────────────────────────────
 */
import { useMemo, useState, type ReactNode } from 'react'
import type { RunArtifact } from '../../api/client'
import type {
  CodePlaneView,
  CostFact,
  DeliveryState,
  ExecCategory,
  RunCostEvidence,
  RunEvidence,
  RunExperienceItem,
  RunMetric,
  RunSection,
  RunSourceRef,
  RunTab,
} from './types'
import { deriveEvidenceSummary, deriveNextActions, isPlatformToolId, outcomeMeta } from './realRun'
import type { DegradeNotice, RunMissingInput } from './realRun'
import { workflowTypeLabel } from './roles'
import { ResultHeadline } from './ResultHeadline'
import { ResultBlockers } from './ResultBlockers'
import { ResultNextActions } from './ResultNextActions'
import { SkillCapture } from './SkillCapture'
import { ResultDetails } from './ResultDetails'
import { CodeTaskTimeline } from './CodeTaskTimeline'
import { CodeApproval } from './CodeApproval'
import { ExecDetailPanel } from './ExecDetailPanel'
import { useArtifactEdit } from './useArtifactEdit'
import { AGENT_RESUME_INSTRUCTION, QUICK_ACTIONS, pickPrimaryArtifact } from './resultActions'

/**
 * INC19 — the four tabs. 结果与过程彻底分家：工程明细（执行记录 / 任务计划 /
 * 未适用 / 受阻 / 失败）不再出现在首屏，而是收进「执行轨迹」，让产物当主角。
 *
 * 纪律：切换 Tab **只改变哪一块在屏幕上**，不重复渲染同一段内容 —— 每一条信息
 * 在整个页面上有且仅有一处归属。
 */
const TABS: { id: RunTab; label: string; hint: string }[] = [
  { id: 'result', label: '结果', hint: '智能体交付的最终成果' },
  { id: 'evidence', label: '证据', hint: '每个数字的依据与来源' },
  { id: 'trace', label: '执行轨迹', hint: '工具、状态、耗时与错误' },
  { id: 'cost', label: '成本与记忆', hint: '模型、用量与经验沉淀' },
]

export type ResultPanelProps = {
  runId: string
  intent: string
  outcome: string
  status: string
  artifacts: RunArtifact[]
  conclusions: string[]
  sources: RunSourceRef[]
  /** INC18 — 第三层：一条证据 = 一条 L2 执行记录（严格 1:1，可逐字对齐）。 */
  evidence: RunEvidence[]
  /**
   * INC18 — 计划承诺但**未执行**的步骤（后端 verdict 同源口径）；INC23 起**不含受阻步骤**
   * （受阻步骤另见 `missingInputs` / `result-missing-inputs`）。为空时 `result-partial` 不渲染。
   */
  unrunSteps: string[]
  /** INC18-B — 成果章节（由产物正文的真实标题切出）。 */
  sections: RunSection[]
  /** INC18-B — 业务指标（**仅**来自产物里的真实业务表格，工程表不算）。 */
  metrics: RunMetric[]
  /** INC18-B — 关键发现（产物正文里真实存在的列表项）。 */
  findings: string[]
  /** INC18-B — 六态交付状态（仅前端派生，不新增后端状态值）。 */
  delivery: { state: DeliveryState; label: string; tone: string }
  /** INC19 — 当前 Tab（由页面持有状态，组件只负责呈现）。 */
  tab: RunTab
  onTabChange: (tab: RunTab) => void
  /** INC19 — 执行轨迹面板的内容（ExecutionSection 整体搬进来，testid 不变）。 */
  trace?: ReactNode
  /**
   * INC19 — 产物正文的**交付部分**（`partitionArtifactBody(content).deliverable`，逐字）。
   * 口径：`result-body` 的读视图渲染它（主产物跟随本地编辑态，见 `ResultDetails`）；
   * 工程账本部分**不在这里**，而在 `ledger`。
   */
  body: string
  /**
   * INC19 — 产物正文的**工程账本部分**（`partitionArtifactBody(content).engineering`，
   * 平台原始、逐字）。口径：只在「执行轨迹」Tab 的 `execution-ledger`（`<pre>` 原文）
   * 内呈现，**不随**本地编辑变化（D-6：账本是平台物证，不可编辑）。
   */
  ledger: string
  /**
   * INC19 / D-9 — 本次运行是否**真的产出了业务交付内容**（交付部分里存在 `## ` 级真实小节）。
   * 口径：来自 `partitionArtifactBody(artifacts[0].content).segments`（**平台原始正文**，
   * 不随本地编辑变化）；为 `false` 时段④ 追加一条诚实说明 `result-no-deliverable`，
   * 如实告知「平台只记录了执行过程，未生成报告正文」。为 `true` 时该说明**不渲染**。
   */
  hasDeliverable: boolean
  /**
   * INC21 / G1 —— 本次运行的「降级说明」（由 `realRun.deriveDegradeNotice(real.llm)` 算好传入）。
   * `present === true` 时段④ 渲染 `result-degrade-note`（诚实说明**为什么**没有交付内容），
   * 且**不再**渲染既有的 `result-no-deliverable`（同一件事只说一遍）；段② 亦优先采用其 `label`。
   */
  degrade: DegradeNotice
  /** INC21 / G3 —— 本次运行的真实步数（`realStages.length`），段① 完成度与页脚「步骤 M」用。 */
  stageCount: number
  /** INC21 / G3 —— 其中已完成（`status === 'done'`）的步数，用于段①「已完成 M / 共 N 步」。 */
  doneStageCount: number
  /**
   * INC20 / T05 —— 成本面板的真实事实。**是否包含** Token/成本两行由页面层
   * 依据「模型驱动证据」判定（`costEvidence.modelDriven`）；本组件只负责呈现。
   */
  costFacts: CostFact[]
  /** INC20 / T05 —— 「本次运行是否调用过模型」的显式判定 + 未调用时的诚实说明。 */
  costEvidence: RunCostEvidence
  /** INC20 / P1-3 —— 本次运行真实存在的经验与记忆（id 级）+ 加载 / 失败态。 */
  experiences: {
    hasExperience: boolean
    pending: boolean
    error: string | null
    items: RunExperienceItem[]
  }
  loading: boolean
  errorLabel?: string | null
  errorDetail?: string
  /**
   * INC22 W3.2 —— 本次运行**受阻**（blocked）步骤的清单（`realRun.deriveMissingInputs`
   * 算好传入）：`blocked_reason` 逐字取自后端。至少 1 条时段④ 渲染 `result-missing-inputs`。
   */
  missingInputs: RunMissingInput[]
  /** 由 LiveRunsView 注入；内部调用 useWorkspaceCreateTask.mutate（`POST /workspace/tasks`）。 */
  onContinue: (nextInstruction: string, context: Record<string, unknown>) => void
  continuePending: boolean
  continueError?: string | null
  /**
   * INC22 W3.3 —— 「重新运行（保留原有声明）」：由 LiveRunsView 注入（内部调用
   * `useReplanRun`，走真实 `POST /runs/{id}/replan`）。按钮**仅当**存在受阻步骤
   * **或**没有交付内容时渲染。失败经 `humanizeError` 由 `rerunError` 如实展示。
   */
  onRerun: () => void
  rerunPending: boolean
  rerunError?: string | null
  /**
   * INC24 / Q5 —— run 级**真实墙钟**（毫秒）：`runWallClockMs(real.created_at, real.completed_at)`。
   * `null` 表示未测量 / 不可解析 ⇒ 段⑥ 页脚**整项省略**「耗时」（绝不写「—」或 0）。
   */
  runDurationMs: number | null
  /**
   * INC25 / T05 —— 代码执行面区块（由 `realRun.deriveCodePlane` 装配）。非代码任务
   * `present === false` ⇒ **不渲染**任何代码区块（普通 run 逐字不变）。
   */
  codeplane: CodePlaneView
  /** INC25 / T05 —— 三动作审批里的批准 / 拒绝（真调 `/codeplane/runs/{id}/approve|reject`）。 */
  onCodeDecision: (action: 'approve' | 'reject') => void
  /** INC25 / T05 —— 第三动作「重新分析」（复用既有 `POST /runs/{id}/replan`）。 */
  onCodeReanalyze: () => void
  codeDecisionPending: boolean
  codeDecisionError?: string | null
  /**
   * INC36 —— L2「查看执行详情」五类结构化字段（`conversation.deriveExecCategories` 由
   * 页面层算好传入）。渲染进既有 `workspace-exec-detail` 的 `<details>` 内、`res-tabs`
   * **之上**；**默认折叠时不进 DOM**（⇒ `conv-exec-*` 默认 `count=0`），展开后出现。
   */
  execCategories: ExecCategory[]
}

export function ResultPanel({
  runId,
  intent,
  outcome,
  artifacts,
  conclusions,
  sources,
  evidence,
  unrunSteps,
  sections,
  metrics,
  findings,
  delivery,
  tab,
  onTabChange,
  trace,
  body,
  ledger,
  hasDeliverable,
  degrade,
  stageCount,
  doneStageCount,
  costFacts,
  costEvidence,
  experiences,
  loading,
  errorLabel,
  errorDetail,
  missingInputs,
  onContinue,
  continuePending,
  continueError,
  onRerun,
  rerunPending,
  rerunError,
  runDurationMs,
  codeplane,
  onCodeDecision,
  onCodeReanalyze,
  codeDecisionPending,
  codeDecisionError,
  execCategories,
}: ResultPanelProps) {
  const primary = pickPrimaryArtifact(artifacts)
  // INC36 —— 「查看执行详情」折叠态由本组件持有（原生 `<details>` 的用户 toggle 会同步）。
  // 目的：让 `ExecDetailPanel` 的 `conv-exec-*` 字段**默认不进 DOM**（⇒ 默认 count=0），
  // 展开后才挂载；切到非「结果」Tab 时导航条本就展开，故此时也挂载。
  const [detailOpen, setDetailOpen] = useState(false)
  const execDetailOpen = detailOpen || tab !== 'result'
  // INC32 修复 —— `deliveryState` 现已覆盖全部终态（含 `aborted`），故这个
  // `outcomeMeta` 兜底**保留但不再触发**（避免下一个人误以为它能兜住 aborted）。
  const label = delivery.label || outcomeMeta(outcome).label
  // INC19 — 工程章节（执行记录 / 任务计划 / 未适用 / 受阻 / 失败）是 Trace，不是交付物。
  const deliverableSections = sections.filter((s) => !s.engineering)
  // INC20 / P1-5 —— 证据 Tab 的计数汇总头：口径 = 两个**已渲染列表**的 `.length`
  // （N = sources.length，M = evidence.length），**仅当 N + M > 0 时渲染**（绝不显示「0 个来源」）。
  const evidenceSummary = deriveEvidenceSummary(sources, evidence)
  // INC21 / G2 —— 结果 Tab／段⑥ 的入口可见条件：**仅当**真实计数 N + M > 0 时渲染。
  const hasEvidence = evidenceSummary.sources + evidenceSummary.evidence > 0
  // INC22 W3.3 —— 「重新运行」入口的可见条件：存在受阻步骤 **或** 没有交付内容。
  const canRerun = missingInputs.length > 0 || !hasDeliverable
  // INC34 —— 「沉淀为技能」入口的可见条件：本次运行**真的**抽取出了经验，且处于
  // 可复盘的状态（失败 / 中止 / 驳回 / 中断 / 等待审批的运行不提供该入口）。
  // 入口组件自身再按角色门控（编译 / 发布分别需 write:skills / approve:skills）。
  const capturable =
    experiences.hasExperience &&
    !['waiting', 'need_approval', 'failed', 'aborted', 'interrupted', 'rejected'].includes(
      delivery.state,
    )
  // INC24 / C6 —— 段⑤「我自己处理」与段⑥ 编辑器**共享**的产物编辑态（由本常驻组件持有）。
  const edit = useArtifactEdit(runId, primary)

  // INC24 / 段⑤ —— 「下一步：A → B → C」+ 三档动作（真实状态派生，不臆造）。
  const derivation = useMemo(
    () => deriveNextActions({ outcome, hasDeliverable, missingInputs, unrunSteps, degrade }),
    [outcome, hasDeliverable, missingInputs, unrunSteps, degrade],
  )
  // INC24 —— 段⑤ 的「继续执行」输入：以 `runId` 门控（换 run 自动清空，不跨 run 泄漏）。
  const [instrState, setInstrState] = useState<{ runId: string; value: string }>({
    runId,
    value: '',
  })
  const nextInstruction = instrState.runId === runId ? instrState.value : ''
  const setNextInstruction = (value: string) => setInstrState({ runId, value })

  // INC24 / INC32 —— 段⑤ 的所有「创建后续运行」通路共用此提交（真调后端
  // `POST /workspace/tasks`，并带 `parent_run_id`，见 `LiveRunsView.tsx::onContinue`）。
  const continueWith = (instruction: string, extra?: Record<string, unknown>) => {
    if (continuePending) return
    onContinue(instruction, {
      continued_from_run_id: runId,
      continued_from_artifact_ref: primary?.result_ref ?? '',
      ...(extra ?? {}),
    })
  }

  const submitContinue = () => {
    const text = nextInstruction.trim()
    if (!text || continuePending) return
    continueWith(text)
    setNextInstruction('')
  }

  // INC24 —— agent 档：有阻塞/无交付 ⇒ 重新运行（replan）；完成态 ⇒ 创建后续运行（tasks）。
  // 二者**都真调后端**（AC-5），所以这不是占位按钮。
  const onAgentAction = () => {
    if (canRerun) {
      onRerun()
      return
    }
    continueWith(AGENT_RESUME_INSTRUCTION)
  }

  // 段③ —— 一句话结论取 `conclusions[0]`（段②），其余结论行在此渲染 ⇒ 不丢不重（§7.6-1）。
  const restConclusions = conclusions.slice(1)

  return (
    <section className="res-panel" data-testid="result-layer" aria-label="任务结果">
      <div className="res-head">
        <span className="res-eyebrow" data-testid="result-layer-title">
          任务结果
        </span>
        <span
          className={`badge ${delivery.tone || outcomeMeta(outcome).tone}`.trim()}
          data-testid="result-delivery"
        >
          {label}
        </span>
      </div>

      {loading ? (
        <ResultSkeleton />
      ) : errorLabel ? (
        <p className="res-error" role="alert" title={errorDetail}>
          {errorLabel}
        </p>
      ) : (
        <>
          {/* INC32 修复 / 需求符合度 —— 「查看执行详情」折叠入口（PRD AC-22 +
              用户需求三处点名）。**只折叠 Tab 导航条本身**：`result` tabpanel 的内容仍
              默认可见（`inc29_code_entries.spec.ts` 依赖默认 `tab='result'` 下 `code-plane`
              / `code-timeline` 可见），**绝不**把 tabpanel 折进去。切到 evidence/trace/cost
              后导航条保持展开（否则回不去「结果」）；默认 `tab === 'result'` ⇒ 无 `open`
              ⇒ 满足 AC-22「默认折叠」。`res-tabs` 类名 / `role="tablist"` / 四个
              `result-tab-*` 逐字不动。 */}
          <details
            className="res-detail-fold"
            data-testid="workspace-exec-detail"
            open={tab !== 'result'}
            onToggle={(e) => setDetailOpen(e.currentTarget.open)}
          >
            <summary>查看执行详情</summary>
            {/* INC36 L2 —— 结构化执行详情（五类「存在性 / 计数 / 状态」+「详细 Trace ›」）。
                仅在展开时挂载：默认折叠 ⇒ `conv-exec-*` 不进 DOM（count=0）；展开后出现。 */}
            {execDetailOpen && (
              <ExecDetailPanel categories={execCategories} onTrace={() => onTabChange('trace')} />
            )}
            {/* INC19 — 四个 Tab。**每一条信息有且仅有一处归属**：工程明细不再和产物
                抢首屏，而是各自进自己的 Tab。Tab 只切换"哪一块在屏幕上"，不复制内容。 */}
            <div className="res-tabs" role="tablist" aria-label="运行视图">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  id={`res-tab-${t.id}`}
                  aria-selected={tab === t.id}
                  aria-controls={`res-panel-${t.id}`}
                  title={t.hint}
                  className={`res-tab${tab === t.id ? ' on' : ''}`}
                  data-testid={`result-tab-${t.id}`}
                  onClick={() => onTabChange(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </details>

          <div
            role="tabpanel"
            id="res-panel-result"
            aria-labelledby="res-tab-result"
            className="res-tabpanel"
            hidden={tab !== 'result'}
          >
            {/* ── ① 任务状态 ─────────────────────────────────────────────── */}
            {/* 一句话人话状态：交付标签 +（有步骤时）完成度。**不含工程词**（P0-5）。 */}
            <p className="res-status-line" data-testid="result-status-line">
              {label}
              {stageCount > 0 ? ` · 已完成 ${doneStageCount} / 共 ${stageCount} 步` : ''}
            </p>
            <div className="res-intent" data-testid="result-intent">
              <span className="res-intent-label">我让智能体做什么</span>
              <p className="res-intent-text">{intent || '（本次运行未记录意图）'}</p>
            </div>

            {/* ── ② 一句话结论（置顶、首屏主角）───────────────────────────── */}
            {/* `conclusions[0]` 逐字；无真实结论时诚实降级（优先 degrade.label，再否则状态复述）。 */}
            <ResultHeadline conclusions={conclusions} degrade={degrade} deliveryLabel={label} />

            {/* ── ③ 核心发现 ─────────────────────────────────────────────── */}
            {/* INC18-B — 指标卡。**只有**产物里真的存在业务表格时才出现；工程表被 deriveMetrics 拒掉。 */}
            {metrics.length > 0 && (
              <section className="res-sect res-metrics" data-testid="result-metrics" aria-label="指标">
                <h4>指标</h4>
                <div className="res-metric-grid">
                  {metrics.map((m, i) => {
                    // INC19 / D-7 S-3 — 平台工具 id（如 research.search）不得占据首屏：把
                    // `rest` 里符合工具 id 句型的单元格**抑制**掉；非工具 id 的 rest 照常渲染。
                    const rest = m.rest.filter((cell) => !isPlatformToolId(cell))
                    return (
                      <div className="res-metric" key={`${m.label}-${i}`}>
                        <span className="res-metric-label">{m.label}</span>
                        <span className="res-metric-value">{m.value}</span>
                        {rest.length > 0 && (
                          <span className="res-metric-rest">{rest.join(' · ')}</span>
                        )}
                      </div>
                    )
                  })}
                </div>
                <p className="res-subtle">以上指标直接取自本次产物的表格，未经加工。</p>
              </section>
            )}

            {/* INC18-B — 关键发现：产物正文里真实存在的列表项，编号呈现。 */}
            {findings.length > 0 && (
              <section
                className="res-sect res-findings"
                data-testid="result-findings"
                aria-label="关键发现"
              >
                <h4>关键发现</h4>
                <ol className="res-find-list">
                  {findings.map((f, i) => (
                    <li key={`${i}-${f}`}>{f}</li>
                  ))}
                </ol>
              </section>
            )}

            {/* INC24 / Q2 —— `result-conclusions` section **恒渲染**，渲染 `conclusions.slice(1)`
                （`conclusions[0]` 已在段② `result-headline`）。空态文案对本情形为真：
                「本次运行未记录其他关键结论」（0 条与 1 条两种情形都成立）。
                不变式（§7.6-1，QA 可测）：`[result-headline] + [...result-conclusions li]`
                === `conclusions` 完整序列，无重复、无丢失。 */}
            <section
              className="res-sect res-concl"
              data-testid="result-conclusions"
              aria-label="关键结论"
            >
              <h4>关键结论</h4>
              {restConclusions.length > 0 ? (
                <ul className="res-concl-list">
                  {restConclusions.map((c, i) => (
                    <li key={`${i}-${c}`}>{c}</li>
                  ))}
                </ul>
              ) : (
                <p className="res-subtle">本次运行未记录其他关键结论</p>
              )}
            </section>

            {/* ── ④ 当前阻塞（仅有内容时渲染；AC-6）──────────────────────────── */}
            <ResultBlockers
              degrade={degrade}
              hasDeliverable={hasDeliverable}
              missingInputs={missingInputs}
              unrunSteps={unrunSteps}
              canRerun={canRerun}
              onRerun={onRerun}
              rerunPending={rerunPending}
              rerunError={rerunError}
            />

            {/* ── ⑤ 下一步动作（恒渲染）─────────────────────────────────────── */}
            <ResultNextActions
              derivation={derivation}
              canRerun={canRerun}
              onAgent={onAgentAction}
              agentPending={canRerun ? rerunPending : continuePending}
              onViewDiff={() => onTabChange('trace')}
              edit={edit}
              hasArtifact={artifacts.length > 0}
              quickActions={QUICK_ACTIONS}
              onQuickAction={continueWith}
              continueValue={nextInstruction}
              onContinueValue={setNextInstruction}
              onSubmitContinue={submitContinue}
              continuePending={continuePending}
              continueError={continueError}
              runId={runId}
            />

            {/* INC34 —— 沉淀为技能（次级、克制、默认收起；仅 manager+ 且有可复盘
                经验时出现）。把一次成功运行的经验一路封装为可复用技能资产。 */}
            {capturable && (
              <SkillCapture
                runId={runId}
                hasExperience={experiences.hasExperience}
                pending={experiences.pending}
                experienceIds={experiences.items.map((x) => x.id)}
              />
            )}

            {/* ── ⑥ 详细证据（可展开；初始无 open）──────────────────────────── */}
            <ResultDetails
              runId={runId}
              artifacts={artifacts}
              body={body}
              sections={deliverableSections}
              evidenceCount={evidence.length}
              stageCount={stageCount}
              runDurationMs={runDurationMs}
              hasEvidence={hasEvidence}
              onTabChange={onTabChange}
              edit={edit}
              primary={primary}
            />

            {/* ── INC25 / T05 —— 代码执行面（仅代码任务出现）─────────────────────
                六段之外**增量**挂载：任务时间线（默认折叠原始 Trace，AC-13）+ 代码
                变更 / 测试结论 / **仅三个**审批动作（AC-18）。非代码任务 `present=false`
                ⇒ 整块不进 DOM（普通 run 逐字不变，AC-24）。 */}
            {codeplane.present && (
              <section className="code-plane" data-testid="code-plane" aria-label="代码执行面">
                <CodeTaskTimeline
                  timeline={codeplane.timeline}
                  affectedSteps={codeplane.affectedSteps}
                  injected={codeplane.injected}
                />
                <CodeApproval
                  approval={codeplane.approval}
                  diff={codeplane.diff}
                  tests={codeplane.tests}
                  onDecision={onCodeDecision}
                  onReanalyze={onCodeReanalyze}
                  pending={codeDecisionPending}
                  error={codeDecisionError ?? null}
                />
              </section>
            )}
          </div>

          {/* ── Tab ② 证据 ───────────────────────────────────────────────── */}
          <div
            role="tabpanel"
            id="res-panel-evidence"
            aria-labelledby="res-tab-evidence"
            className="res-tabpanel"
            hidden={tab !== 'evidence'}
          >
            {/* INC20 / P1-5 —— 计数汇总头。仅当 N + M > 0 时渲染（防假计数：口径就是
                两个已渲染列表的 .length）。 */}
            {evidenceSummary.sources + evidenceSummary.evidence > 0 && (
              <p className="res-ev-sum-head" data-testid="result-evidence-summary">
                {evidenceSummary.sources} 个来源 · {evidenceSummary.evidence} 条数据依据
              </p>
            )}
            {/* INC18 — 第三层：数据依据。一条 = 一条 L2 记录，可展开看真实返回，
                让用户能对每个数字「查看依据」。没有记录就是没有依据。 */}
            <section
              className="res-sect res-evidence"
              data-testid="result-evidence"
              aria-label="数据依据"
            >
              <h4>数据依据</h4>
              {evidence.length > 0 ? (
                <ul className="res-ev-list">
                  {evidence.map((e) => (
                    <li className="res-ev-item" key={e.id}>
                      <details>
                        <summary className="res-ev-sum">
                          {/* INC20 / P1-1 —— 主标识改用**业务标识**（角色展示名 / 真实
                              工具 id，与工作链一致）；原始工具 id 保留在 `title` 里可回溯。 */}
                          <span className="res-ev-tool" title={e.tool}>
                            {e.bizLabel}
                          </span>
                          <span className={`res-ev-status ${e.executed ? 'ok' : 'no'}`}>
                            {e.statusLabel}
                          </span>
                          <span className="res-ev-ms">
                            {e.ms === null ? '—' : `${e.ms} ms`}
                          </span>
                        </summary>
                        {e.snippet ? (
                          <pre className="res-ev-snippet">{e.snippet}</pre>
                        ) : (
                          <p className="res-subtle">该次调用没有记录返回内容</p>
                        )}
                        {e.resultRef && <p className="res-ev-ref">证据指纹 {e.resultRef}</p>}
                      </details>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="res-subtle">本次运行未记录可展示的数据依据</p>
              )}
            </section>

            <section className="res-sect res-sources" data-testid="result-sources" aria-label="来源">
              <h4>来源</h4>
              {sources.length > 0 ? (
                <ul className="res-src-list">
                  {sources.map((s, i) => (
                    <li key={`${s.kind}-${i}`}>
                      <span className="res-src-kind">{s.kind}</span>
                      <span className="res-src-label">{s.label}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="res-subtle">本次运行未记录可展示的来源</p>
              )}
            </section>
          </div>

          {/* ── Tab ③ 执行轨迹 ───────────────────────────────────────────── */}
          {/* INC19 — 既有 ExecutionSection 整体迁入（testid 不变：execution-layer
              / -title / -body）。provider / 耗时 / 状态 / 未适用 / 受阻 / 失败
              只在这里出现 —— 首屏不再有任何工程字段。 */}
          <div
            role="tabpanel"
            id="res-panel-trace"
            aria-labelledby="res-tab-trace"
            className="res-tabpanel"
            hidden={tab !== 'trace'}
          >
            {/* INC19 / INC20 —— 轨迹 Tab = 执行链（`ExecutionSection`，**进入本 Tab 即
                默认展开**）+ 平台**原始账本原文**（`execution-ledger`，`<details>` **仍默认
                折叠**）+ 节点内支撑细节（仍默认折叠）。
                INV-19-1′（修订后口径）：`「默认折叠」的作用域 = 平台原始账本容器 + 节点级
                支撑细节容器；工作链节点列表在用户**进入**「执行轨迹」Tab 后默认展开`。
                断言强度不变：`execution-ledger` 的 `<details>` 仍**无** `open`。 */}
            <div className="res-trace-stack">
              {trace ?? (
                <p className="res-subtle">本次运行没有可展示的执行轨迹</p>
              )}
              {ledger && (
                <div className="res-ledger" data-testid="execution-ledger">
                  <details>
                    <summary className="res-ledger-sum">查看平台原始执行账本（原文）</summary>
                    {/* 必须是 <pre> 原样文本 —— 走 ResultMarkdown 会把 `##` 转成 <h2>，
                        令 textContent 丢掉 `##` 字面，AC-9 的逐字断言会失败（R-9.7）。 */}
                    <pre className="res-ledger-body">{ledger}</pre>
                  </details>
                  <p className="res-subtle" data-testid="result-ledger-note">
                    以上为平台原始记录，<strong>不随</strong>上方本地编辑变化。
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* ── Tab ④ 成本与记忆 ─────────────────────────────────────────── */}
          {/* INC20 / T05 —— 只渲染后端**真有**的字段；且「未测量 ≠ 0」：
              * 是否渲染 Token/成本由页面层的**模型驱动证据**判定（`costEvidence`），
                **不再**用 `typeof === 'number'`（生产者在确定性档也写 0，数值不可分）；
              * 非模型驱动 ⇒ 只出「运行档位」+ 一条**诚实说明**（`result-cost-nomodel`）。 */}
          <div
            role="tabpanel"
            id="res-panel-cost"
            aria-labelledby="res-tab-cost"
            className="res-tabpanel"
            hidden={tab !== 'cost'}
          >
            {costFacts.length > 0 ? (
              <ul className="res-cost-list" data-testid="result-cost">
                {costFacts.map((f) => (
                  <li key={f.label} className="res-cost-item">
                    <span className="res-cost-label">{f.label}</span>
                    <span className="res-cost-value">{f.value}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="res-subtle">本次运行未记录用量与成本数据</p>
            )}

            {/* 未调用模型 ⇒ 诚实说明（不是 fact 行，故不与 AC-5 的计数标签冲突）。 */}
            {!costEvidence.modelDriven && costEvidence.note && (
              <p className="res-cost-note" data-testid="result-cost-nomodel" role="status">
                {costEvidence.note}
              </p>
            )}

            {/* INC20 / P1-3 —— 本次运行**真实存在**的经验与其关联记忆（**id 级**）。
                仅在确有 `experience_id` 时才请求；失败诚实降级（不显示伪值）；无条目 ⇒
                诚实空态。`Skill 使用情况 / Memory 内容级 / 严格上下文摘要` 无数据源，本期不做。 */}
            {experiences.hasExperience && (
              <section
                className="res-sect res-experiences"
                data-testid="result-experiences"
                aria-label="经验与记忆"
              >
                <h4>经验与记忆</h4>
                {experiences.pending ? (
                  <p className="res-subtle">正在加载经验与记忆…</p>
                ) : experiences.error ? (
                  <p className="res-error" role="alert">
                    {experiences.error}
                  </p>
                ) : experiences.items.length > 0 ? (
                  <ul className="res-exp-list">
                    {experiences.items.map((x) => {
                      // INC21 / G5 —— 标签经 `workflowTypeLabel` 映射为**业务表述**
                      // （`generic` ⇒「通用流程」等），**不再**直出平台内部枚举。
                      const tagLabels = x.tags
                        .map((t) => workflowTypeLabel(t))
                        .filter((l) => l.length > 0)
                      return (
                        <li key={x.id} className="res-exp-item">
                          <p className="res-exp-summary">
                            {x.summary || '（本次经验未记录摘要）'}
                          </p>
                          <p className="res-exp-meta">
                            结论：{outcomeMeta(x.outcome).label}
                            {tagLabels.length > 0 ? ` · 标签：${tagLabels.join('、')}` : ''}
                          </p>
                          {x.memoryIds.length > 0 ? (
                            <p className="res-exp-mem">
                              关联记忆 {x.memoryIds.length} 条：
                              {x.memoryIds.map((id) => id.slice(0, 8)).join('、')}
                            </p>
                          ) : (
                            <p className="res-subtle">本次经验未关联记忆条目</p>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                ) : (
                  <p className="res-subtle">本次运行未检索到可展示的经验与记忆</p>
                )}
              </section>
            )}
          </div>
        </>
      )}
    </section>
  )
}

/** Loading skeleton — structure only, never fake data (P1-3). */
function ResultSkeleton() {
  return (
    <div className="res-skeleton" role="status" aria-live="polite" aria-label="结果加载中">
      <span className="res-skel-line w60" />
      <span className="res-skel-line w90" />
      <span className="res-skel-line w80" />
      <span className="res-skel-line w40" />
    </div>
  )
}
