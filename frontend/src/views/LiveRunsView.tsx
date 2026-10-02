/**
 * 智能任务 (/tasks) — the session workspace (INC32 / T04: three columns).
 *
 * Layout (INC32-DESIGN ADR-08): the page evolves **in place** into a three-column
 * workspace, the export name is unchanged so `router.tsx` needs no edit:
 *   左列 历史 · RunListPanel(+会话分组)      — pick / create a run
 *   中列 我的任务 → 执行状态 → 最终结果      — RunHeader + WorkspaceLiveStrip(仅运行中)
 *                                             + 停止(运行中, 可执行身份) + ResultPanel(四 Tab 原样)
 *   右列 产物 · ArtifactPanel               — 产物卡 + 逐字预览 + 下载
 *
 * INC32 / T05 —— 接线：中列实时业务语步骤流（`WorkspaceLiveStrip`，消费既有
 * `useRunEvents`）；运行中「停止」（`POST /runs/{id}/abort`）；「继续执行」真调
 * `POST /workspace/tasks` 并带 `parent_run_id`（真实 Follow-up，AC-39/AC-41）。
 *
 * REAL DATA ONLY (INC14/INC32): 左列历史为**两源合并**（`runs/history.ts` 纯函数）——
 * 持久底 `GET /workspace/sessions`（`useWorkspaceSessions`，跨重启真实存在）+ 易失补充
 * `GET /runs`（`useHubRuns`，运行中 / 新建 / follow-up 的实时来源），按 `run_id` 去重、
 * 持久项为准。详情仍走 `GET /runs/{id}`（`useRunDetail` 不变）。租户确无任何历史时左列
 * 显示诚实空态（`session-history-empty`「暂无历史任务」）—— 绝不伪造内容。
 */
import { useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useParams } from '@tanstack/react-router'
import { humanizeError } from '../api/errors'
import type { RunLLM } from '../api/client'
import { useAbortRun, useCodeDecision, useReplanRun, useRunDetail, useRunExperiences, useWorkspaceCreateTask } from '../api/hooks'
import { useSession } from '../hooks/useSession'
import { roleConfigFor } from '../home/roleConfig'
import type { CodePlaneView, CostFact, RunCostEvidence, RunExperienceItem, RunTab, ViewMode } from './runs/types'
import { useViewMode } from './runs/useViewMode'
import {
  deliveryState,
  deriveArtifacts,
  deriveCodePlane,
  deriveConclusions,
  deriveDegradeNotice,
  deriveEvidence,
  deriveFindings,
  deriveMetrics,
  deriveMissingInputs,
  deriveSections,
  deriveSources,
  deriveUnrunStepLabels,
  detailToStages,
  partitionArtifactBody,
  projectExperience,
  runDebugFacts,
  runStatusMeta,
  runWallClockMs,
} from './runs/realRun'
import { isModelDriven, runtimeModeLabel } from './runs/roles'
// INC36 — 会话工作台「ChatGPT 式分层」的 L1/L2 纯函数 + 组件。
import { deriveExecCategories, deriveSourceRows, stagesToStepData } from './runs/conversation'
// P0 —— 左列历史两源合并（持久会话为底 + 易失运行中项补充）的纯函数层。
import { useMergedHistory } from './runs/useMergedHistory'
import { RunListPanel } from './runs/RunListPanel'
import { ResultPanel } from './runs/ResultPanel'
import { pickPrimaryArtifact } from './runs/resultActions'
import { ExecutionSection } from './runs/ExecutionSection'
import { ArtifactPanel } from './runs/ArtifactPanel'
import { WorkspaceLiveStrip } from './runs/WorkspaceLiveStrip'
// INC-INLINE-STREAMING —— /tasks 也打字机（D8）：运行中在会话列、实时条上方渲染答案。
import { InlineAnswer } from './runs/InlineAnswer'
import { useRunEvents } from '../hooks/useRunEvents'
import { ModelStatus } from './runs/ModelStatus'
import { ConversationTurn } from './runs/ConversationTurn'
import { AgentRunSummary } from './runs/AgentRunSummary'
import { InlineArtifacts } from './runs/InlineArtifacts'
import { SourcesDisclosure } from './runs/SourcesDisclosure'
import { FollowUpComposer } from './runs/FollowUpComposer'
import './runs/workspace.css'
import '../styles/runs.css'

/**
 * INC25 / T05 —— 「非代码任务 / 尚未加载」时的代码执行面空值（`present=false`）。
 * 结果层据此**不渲染**任何代码区块；各字段给的是诚实的空（`null` / `[]` / `''`），
 * 不是编造的 0 或占位数据。
 */
const EMPTY_CODE_PLANE: CodePlaneView = {
  present: false,
  degraded: null,
  engineAvailable: null,
  engineReason: '',
  workspaceId: '',
  workspaceState: '',
  timeline: [],
  diff: { present: false, text: '', files: [] },
  tests: { measured: false, verdict: 'unmeasured', passed: 0, failed: 0, errors: 0, failedCases: [], command: '' },
  approval: { status: '', approvalId: '', decidedBy: '', decidedAt: '', committed: false },
  summary: { filesChanged: null, testCommand: null, passed: null, failed: null, repairRounds: null },
  affectedSteps: [],
  completedSteps: [],
  injected: { skills: [], memory: [] },
}

export function LiveRunsView() {
  const [mode, setMode] = useViewMode()

  // P0 —— 左列历史切换到**持久数据源**（两源合并）。
  // INC-INLINE-STREAMING / E3 / P7 —— 合并逻辑抽成**唯一**的 `useMergedHistory(limit)`
  // （`useWorkspaceSessions`(持久底) + `useHubRuns`(易失补充) → `mergeHistoryRuns`），
  // 与首页「近期任务」**共用同一实现**（防漂移；B3 的根因是各写一份）。此处 `limit=20`，
  // 行为与改前**逐字等价**（同两源、同合并、加载态以持久底为准）。
  const { runs: list, loading: historyLoading } = useMergedHistory(20)
  // INC36 / T04 —— 深链播种：`/tasks/$runId`（或 `/runs/$runId`）直接打开某个 run 的会话。
  // `strict: false` 让本组件在 `/tasks`（无参）与 `/tasks/<id>`（有参）下都能读 params。
  const params = useParams({ strict: false }) as { runId?: string }
  const routeRunId = typeof params.runId === 'string' && params.runId ? params.runId : null
  const [pickedId, setPickedId] = useState<string | null>(routeRunId)

  // Derived, not stored: a stale pick (run deleted / list reloaded) falls back
  // to the newest run without an effect that could loop on array identity.
  const selectedId =
    pickedId && list.some((r) => r.run_id === pickedId) ? pickedId : list[0]?.run_id ?? null

  const detail = useRunDetail(selectedId)
  const real = detail.data ?? null
  // INC-INLINE-STREAMING —— 单 run 单 SSE：订阅由本组件持有（`WorkspaceLiveStrip` 已改受控
  // props），既供步骤条，也供打字机 `InlineAnswer` 消费 `deltas`（避免重复订阅）。
  const liveRunId = real?.run_id ?? selectedId ?? null
  const {
    events: liveEvents,
    deltas: liveDeltas,
    done: liveDone,
    error: liveError,
  } = useRunEvents(liveRunId)
  // INC19 — 分区：把产物正文按**真实二级标题**分成「交付 / 工程」两桶（纯函数、逐字无损）。
  // 交付部分 → 结果 Tab（result-body / 目录 / 结论 / 发现 / 指标）；工程部分 → 执行轨迹
  // Tab 的账本原文。分桶在页面层统一完成，下游只消费分好的桶（S-1 的机械化保证：所有
  // 取数的入参都从 artifacts[0].content 改为 part.deliverable）。
  const part = useMemo(
    () => partitionArtifactBody(real?.artifacts?.[0]?.content ?? ''),
    [real],
  )
  // INC19 / D-9 —— 本次运行是否**真的产出了业务交付内容**：交付部分里存在 `## ` 级真实
  // 小节（`title !== ''`）即为真；前言段（`# 运行报告` + `**意图**：…`）的 `title === ''`，
  // 不计入。判定**只依赖分区区间见证**，不看文案 / 不看长度 / 不猜语义（可证伪、无启发式）。
  // 口径：它描述的是「**平台**产出了什么」（平台事实），故基于 `part`（平台原始正文），
  // **不**基于本地编辑后的 `saved` —— 不应随本地编辑变化。
  const hasDeliverableContent = part.segments.some(
    (s) => s.kind === 'deliverable' && s.title !== '',
  )
  // INC19 — 四 Tab 信息架构：结果是主角，过程是证据。Tab 状态由页面持有。
  const [tab, setTab] = useState<RunTab>('result')
  // INC20 / T04 —— 工作链的**受控展开态**由本组件持有（不是 ExecutionSection 内部）。
  // 原因（致命陷阱，勿删）：四个 Tab 面板用 `hidden` 控制显隐，切 Tab **不会**卸载/
  // 重挂载 `ExecutionSection`，因此「进入 trace Tab 即读一次 defaultOpen」在首次挂载
  // （tab==='result'）后永久失效、功能静默失效。改为受控：切到 trace 时置 true。
  const [traceOpen, setTraceOpen] = useState(false)
  const onTabChange = (next: RunTab) => {
    setTab(next)
    // 语义选定：**进入**「执行轨迹」Tab 即展开工作链（用户点 Tab = 显式请求看过程）；
    // 用户仍可手动折叠；再次切回该 Tab 会重新展开（=「进入即展开」）。
    if (next === 'trace') setTraceOpen(true)
  }

  const realStages = useMemo(() => (real ? detailToStages(real) : []), [real])
  // INC21 / G1 —— 本次运行的「降级说明」：读后端 `llm.degraded`
  // （`realRun.deriveDegradeNotice`），把「为什么没有交付内容」如实告诉用户。
  // INC22 W3.4 —— 额外传入 `runtime_mode`：后端没写 `degraded` 但本次运行**不属于
  // 模型驱动档**（离线编排档）时，也要如实说明「未启用模型驱动」。
  // INC25 / T05 —— 额外传入 `codeplane`：代码执行面的降级（`codeplane.degraded`，
  // 如 `engine_unavailable` / `model_unavailable`）与既有 `llm.degraded` 并行消费
  // （业务文案 / diagnostic 纪律不变；普通 run 的 codeplane 为空 ⇒ 行为逐字不变）。
  const degrade = useMemo(
    () => deriveDegradeNotice(real?.llm, real?.runtime_mode, real?.codeplane),
    [real],
  )
  // INC25 / T05 —— 代码执行面区块（时间线 / Diff / 测试 / 审批）。非代码任务
  // `present === false` ⇒ 结果层不渲染任何代码区块。
  const codeplane = useMemo(
    () => (real ? deriveCodePlane(real) : EMPTY_CODE_PLANE),
    [real],
  )
  // INC21 / G3 —— 结果 Tab「执行轨迹」入口的**真实计数**（共 N 步 / 已完成 M 步）。
  // N = 工作链节点数（`realStages.length`）；M = 其中 `status === 'done'` 的节点数。
  const stageCount = realStages.length
  const doneStageCount = useMemo(
    () => realStages.filter((s) => s.status === 'done').length,
    [realStages],
  )
  const artifacts = useMemo(() => (real ? deriveArtifacts(real) : []), [real])
  const conclusions = useMemo(() => (real ? deriveConclusions(part.deliverable) : []), [real, part])
  const sources = useMemo(() => (real ? deriveSources(real) : []), [real])
  // INC36 —— L1 ③ AI 执行摘要（终态真实步骤 → 业务语 ✓ 行）、L2「查看执行详情」结构化字段、
  // L2「查看来源」折叠行（≤5，全量走 L3 证据 Tab）。全部为纯函数派生，组件只消费。
  const execSteps = useMemo(() => stagesToStepData(realStages), [realStages])
  const execCategories = useMemo(() => (real ? deriveExecCategories(real) : []), [real])
  const sourceRows = useMemo(() => (real ? deriveSourceRows(real, 5) : []), [real])
  // INC18 — 第三层（数据依据，1:1 派生自 L2）+「计划承诺但未执行」的步骤。
  // INC19 / D-4：未完成步骤改出**业务名**（`deriveUnrunStepLabels`），不再直出 `step.tool`。
  const evidence = useMemo(() => (real ? deriveEvidence(real) : []), [real])
  const unrunSteps = useMemo(() => (real ? deriveUnrunStepLabels(real) : []), [real])
  // INC22 W3.2 —— 本次运行**受阻**（blocked）步骤清单（业务名 + `blocked_reason` 原文）。
  const missingInputs = useMemo(() => (real ? deriveMissingInputs(real) : []), [real])
  // INC18-B — 第二层：把产物正文结构化。**只**读**交付部分**，因此正文里没有的东西界面上
  // 不会出现；工程节已被分区移出，天然不可能再被当「成果结构 / 指标 / 关键发现」。
  const sections = useMemo(() => (real ? deriveSections(part.deliverable) : []), [real, part])
  const metrics = useMemo(() => (real ? deriveMetrics(part.deliverable) : []), [real, part])
  const findings = useMemo(() => (real ? deriveFindings(part.deliverable) : []), [real, part])
  const delivery = useMemo(
    () => (real ? deliveryState(real) : { state: 'completed' as const, label: '', tone: '' }),
    [real],
  )

  // INC20 / T05 —— 「成本与记忆」诚实化：
  //   * 「是否跑过模型」的**唯一判据** = 模型驱动证据（`isModelDriven`），**不用**
  //     `typeof total_tokens === 'number'`（生产者在确定性档也写 0，数值不可分）。
  //   * 非模型驱动 ⇒ **不**产出 Token/成本两行（⇒ AC-5「渲染节点数 == 0」成立），
  //     改出一条诚实说明 `result-cost-nomodel`，文案**不含**「Token 用量」「模型成本」
  //     字样（否则 AC-5 计数会被自身说明打红）。
  //   * 「运行档位」恒产出，且走业务文案（`runtimeModeLabel`），**绝不**直出
  //     `deterministic` / `llm` 这类工程值。
  //
  // ✅ P0-5 文案纪律的**正式裁定**（INC37，用户已确认）：用户明确选择**保留 `Token`**
  // （视作成本计量单位，而非内部实现细节），故「Token 用量」「模型成本」两个字面**长期保留**，
  // 不再计为待办：
  //   ① 用户需求里明确要求以 `Token` 作为成本计量单位展示；
  //   ② AC-5 正以该**字面**为计数锚点（改名会让计数口径失锚）。
  // 真正需要处理的是**档位值**：`runtime_mode` 的原始工程值（`deterministic`/`llm`/
  // `react`/`graph`）**绝不直出**，一律经 `runtimeModeLabel` 映射为业务表述。
  const costEvidence = useMemo<RunCostEvidence>(() => {
    if (!real) return { modelDriven: false, note: null }
    const modelDriven = isModelDriven(real.runtime_mode, real.llm)
    return {
      modelDriven,
      note: modelDriven ? null : '本次运行未调用模型，无用量与成本记录',
    }
  }, [real])
  const costFacts = useMemo<CostFact[]>(() => {
    if (!real) return []
    const facts: CostFact[] = []
    if (costEvidence.modelDriven) {
      // 模型驱动档：0 是**真实计量**（已测量），可渲染。
      if (typeof real.total_tokens === 'number') {
        facts.push({ label: 'Token 用量', value: real.total_tokens.toLocaleString() })
      }
      if (typeof real.total_cost_usd === 'number') {
        facts.push({ label: '模型成本', value: `$${real.total_cost_usd.toFixed(4)}` })
      }
    }
    if (real.runtime_mode) facts.push({ label: '运行档位', value: runtimeModeLabel(real.runtime_mode) })
    return facts
  }, [real, costEvidence])

  // INC20 / P1-3 —— 本次运行的**真实经验与记忆**（id 级）。仅在确有 `experience_id`
  // 时才发起 `GET /experiences?run_id=`（避免无谓往返）；失败诚实降级，不显示伪值。
  const runExperiences = useRunExperiences(real?.experience_id ? real.run_id : null)
  const experiences = useMemo(
    () => ({
      hasExperience: !!real?.experience_id,
      pending: !!real?.experience_id && runExperiences.isPending,
      error: runExperiences.isError
        ? humanizeError(runExperiences.error, '经验与记忆加载失败').label
        : null,
      items: (runExperiences.data?.items ?? []).map(projectExperience) as RunExperienceItem[],
    }),
    [
      real?.experience_id,
      runExperiences.data,
      runExperiences.isPending,
      runExperiences.isError,
      runExperiences.error,
    ],
  )

  // INC19 — ExecutionSection 整体迁入「执行轨迹」Tab（testid 不变，只是换了家）。
  // 账本原文块（`execution-ledger`）由 ResultPanel 在 `trace` 之后追加（消费 `ledger`
  // prop），二者在同一个 `res-trace-stack` 内竖向堆叠。INC20 / T04：工作链受控展开
  // （进入 trace Tab 即展开），`execution-ledger` 仍**默认折叠**（<details> 无 open）。
  const tracePanel = (
    <ExecutionSection
      stages={realStages}
      mode={mode}
      open={traceOpen}
      onToggle={() => setTraceOpen((v) => !v)}
    />
  )

  // INC32 / T05 —— 任务创建改走异步通路 `POST /workspace/tasks`
  // （`useWorkspaceCreateTask`）：立即返回句柄、边跑边看；`POST /tasks` 后端不变。
  const create = useWorkspaceCreateTask()
  const continueError = create.error ? humanizeError(create.error, '继续执行失败').label : null

  // INC32 / T05 —— 「继续执行」真调 `POST /workspace/tasks`，并带**真实**
  // `parent_run_id = 当前 run_id`（AC-39：父子关系由真实请求参数承载，不是前端 state
  // 拼接）。`context` 里仍带 `continued_from_run_id`，让后端据此把上一轮上下文真正注入
  // 新 run 的规划（AC-40 的承重路径）。成功后选中新 run，其结果层随之上屏（design §4.1）。
  const onContinue = (nextInstruction: string, context: Record<string, unknown>) => {
    create.mutate(
      { intent: nextInstruction, context, parentRunId: real?.run_id ?? undefined },
      { onSuccess: (handle) => setPickedId(handle.run_id) },
    )
  }

  // INC39 / G —— 中列底部的 follow-up（`conv-followup`）是**统一续聊入口**：它复用 `onContinue`
  // （真调 `POST /workspace/tasks`，带 `parent_run_id`），并**补上**主产物指纹
  // `continued_from_artifact_ref` —— 该参数原本由已退役的 `result-continue` 携带，包一层后
  // 保证**能力不减少**（既有 context 参数一个不丢）。
  const onFollowUp = (nextInstruction: string, context: Record<string, unknown>) => {
    onContinue(nextInstruction, {
      ...context,
      continued_from_artifact_ref: pickPrimaryArtifact(artifacts)?.result_ref ?? '',
    })
  }

  // INC22 W3.3 —— 「重新运行（保留原有声明）」走真实 `POST /runs/{id}/replan`；成功后
  // 选中新 run（沿用页面上既有的 `setPickedId` 机制）。失败经 `humanizeError` 如实展示，
  // **不吞错**。`replan` 复用 `useReplanRun`（与 `useWorkspaceCreateTask` 同款写法）。
  const replan = useReplanRun()
  const rerunError = replan.error ? humanizeError(replan.error, '重新运行失败').label : null
  const onRerun = () => {
    const id = real?.run_id
    if (!id || replan.isPending) return
    replan.mutate(
      { runId: id, reason: '从结果页重新运行（保留原有声明）' },
      { onSuccess: (handle) => setPickedId(handle.run_id) },
    )
  }

  // INC25 / T05 —— 代码任务审批：批准 / 拒绝真调 `/codeplane/runs/{id}/approve|reject`
  // （批准触发复跑，产出代码产物并选中新 run；拒绝销毁工作区）。第三动作「重新分析」
  // 复用既有 replan（即 `onRerun`，走 `POST /runs/{id}/replan`）。失败经 `humanizeError`
  // 如实展示，**不吞错**、**不假装成功**。
  const codeDecision = useCodeDecision()
  const codeDecisionError = codeDecision.error
    ? humanizeError(codeDecision.error, '审批操作失败').label
    : null
  const onCodeDecision = (action: 'approve' | 'reject') => {
    const id = real?.run_id
    if (!id || codeDecision.isPending) return
    codeDecision.mutate(
      { runId: id, action },
      { onSuccess: (handle) => setPickedId(handle.run_id) },
    )
  }

  // INC32 / T05 —— 停止动作（`POST /runs/{id}/abort`，ADR-04）。仅**运行中**且身份可
  // 执行时渲染（只读 viewer 不渲染，不是禁用死按钮，AC-34/AC-37）。失败经
  // `humanizeError` 如实展示真实码（403/404/409），**不吞错、不假装成功**。
  const abort = useAbortRun()
  const stopError = abort.error ? humanizeError(abort.error, '停止任务失败') : null
  const session = useSession()
  const canExecute = roleConfigFor(session?.role).canExecute
  // 运行中 = 交付状态仍在等待（`realRun.deliveryState` 的 'waiting'；终态不残留，AC-20）。
  const running = delivery.state === 'waiting'
  const onStop = () => {
    const id = real?.run_id
    if (!id || abort.isPending) return
    abort.mutate(id)
  }

  const title = real ? real.intent || real.run_id : '加载中…'
  const status = runStatusMeta(real?.status)
  const meta = real
    ? mode === 'debug'
      ? runDebugFacts(real)
      : `${real.run_id.slice(0, 8)} · ${real.status}`
    : ''

  const detailErr = detail.isError ? humanizeError(detail.error, '运行详情加载失败') : null

  return (
    <section className="runs-page" data-screen-label={`Run · ${real?.run_id ?? ''}`}>
      <div className="workspace-columns" data-testid="workspace-columns">
        {/* 左列 —— 历史：运行列表 + 会话分组（复用 RunListPanel，含 ResourcePicker / 声明区）。 */}
        <aside
          className="workspace-col workspace-col-history"
          data-testid="workspace-col-history"
        >
          <RunListPanel
            runs={list}
            loading={historyLoading}
            selectedId={selectedId}
            onSelect={setPickedId}
          />
        </aside>

        {/* 中列 —— 我的任务 → 执行状态 → 最终结果（DOM 顺序即语义顺序，AC-16）。 */}
        <main
          className="workspace-col workspace-col-conversation"
          data-testid="workspace-col-conversation"
        >
          {detailErr && (
            <p className="af-note warn" role="alert" title={detailErr.detail}>
              {detailErr.label}
            </p>
          )}

          {historyLoading ? (
            <div className="workspace-empty">
              <div className="skel" style={{ height: 64, width: '100%' }} />
            </div>
          ) : !selectedId ? (
            /* 诚实空态（替代 demo）：租户确无任何 run。 */
            <div className="workspace-empty" data-testid="workspace-empty-runs">
              <div className="workspace-empty-title">暂无运行记录</div>
              <p className="workspace-empty-hint">在左侧输入一句话运行第一个任务。</p>
            </div>
          ) : (
            <>
              {/* ① 我的任务（标题）+ ② 执行状态（状态徽标 / 元信息 / 模型只读入口） */}
              <RunHeader
                title={title}
                meta={meta}
                status={status}
                mode={mode}
                onMode={setMode}
                llm={real?.llm}
              />

              {/* INC33 —— 重启回填的诚实说明。详情 `detail_retained === false` 表示这条 run
                  是从持久化的运行头回填的：步骤 / 工具调用 / 时间线**未随本次进程保留**。
                  此时如实告知，**不**把「明细未保留」渲染成「没有步骤」的自信空态
                  （ADR-02「跨重启可查」的诚实边界）。老 payload 无此字段 ⇒ 不渲染。 */}
              {real && real.detail_retained === false && (
                <p className="af-note" role="note" data-testid="run-detail-not-retained">
                  该运行的执行明细（步骤 / 工具调用 / 时间线）未随本次进程保留，仅保留运行状态与产物。
                </p>
              )}

              {/* ② 执行状态 —— 运行中时提供停止；只读身份不渲染（AC-34/AC-37）。 */}
              {running && canExecute && (
                <div className="runs-stop-row">
                  <button
                    type="button"
                    className="btn sm danger"
                    data-testid="workspace-stop"
                    onClick={onStop}
                    disabled={abort.isPending}
                  >
                    {abort.isPending ? '停止中…' : '停止任务'}
                  </button>
                  {stopError && (
                    <span className="af-note warn" role="alert" title={stopError.detail}>
                      {stopError.label}
                    </span>
                  )}
                </div>
              )}

              {/* ── INC36 L1 ①「用户消息块」——中列对话时间线的起点（用户 ← → Agent）─── */}
              {real && <ConversationTurn intent={real.intent} createdAt={real.created_at} />}

              {/* ── INC36 L1 ③「AI 执行摘要」──────────────────────────────────────
                  运行中 = 实时业务语步骤流（既有 `WorkspaceLiveStrip`，消费 SSE）；
                  终态   = 本次运行的**真实步骤** ✓ 业务语列表（新增 `AgentRunSummary`）。 */}
              {running ? (
                // INC-INLINE-STREAMING / D8 —— 打字机插在会话列内、实时条**上方**；
                // `InlineAnswer` 在无 answer 文本时不渲染任何 DOM 节点（不移动/替换既有节点，
                // 既有 e2e 对该列 DOM 的断言不回归）。
                <>
                  <InlineAnswer deltas={liveDeltas} streaming={liveDeltas.streaming} />
                  <WorkspaceLiveStrip
                    events={liveEvents}
                    done={liveDone}
                    error={liveError}
                    mode={mode}
                  />
                </>
              ) : (
                <AgentRunSummary steps={execSteps} mode={mode} />
              )}

              {/* ── INC36 L2「查看来源」——默认折叠；诚实 URL 列表（后端无 file/sheet/rows）── */}
              {real && (
                <SourcesDisclosure
                  rows={sourceRows}
                  total={sources.length}
                  onOpenAll={() => onTabChange('evidence')}
                />
              )}

              {/* ③ 最终结果 —— ResultPanel 四 Tab 原样（testid 不变）。 */}
              <div className="runs-body">
                <ResultPanel
                  runId={real?.run_id ?? selectedId ?? ''}
                  intent={real?.intent ?? ''}
                  outcome={real?.outcome ?? ''}
                  status={real?.status ?? ''}
                  artifacts={artifacts}
                  conclusions={conclusions}
                  sources={sources}
                  evidence={evidence}
                  unrunSteps={unrunSteps}
                  sections={sections}
                  metrics={metrics}
                  findings={findings}
                  delivery={delivery}
                  tab={tab}
                  onTabChange={onTabChange}
                  trace={tracePanel}
                  body={part.deliverable}
                  ledger={part.engineering}
                  hasDeliverable={hasDeliverableContent}
                  degrade={degrade}
                  stageCount={stageCount}
                  doneStageCount={doneStageCount}
                  costFacts={costFacts}
                  costEvidence={costEvidence}
                  experiences={experiences}
                  loading={detail.isPending && !!selectedId}
                  errorLabel={detailErr?.label ?? null}
                  errorDetail={detailErr?.detail}
                  missingInputs={missingInputs}
                  onContinue={onContinue}
                  continuePending={create.isPending}
                  onRerun={onRerun}
                  rerunPending={replan.isPending}
                  rerunError={rerunError}
                  runtimeMode={real?.runtime_mode}
                  runDurationMs={runWallClockMs(real?.created_at, real?.completed_at)}
                  codeplane={codeplane}
                  onCodeDecision={onCodeDecision}
                  onCodeReanalyze={onRerun}
                  codeDecisionPending={codeDecision.isPending}
                  codeDecisionError={codeDecisionError}
                  execCategories={execCategories}
                />
                {mode === 'debug' && real && (
                  <details className="run-raw">
                    <summary>原始运行数据（真实响应）</summary>
                    <pre className="raw-json">{JSON.stringify(real, null, 2)}</pre>
                  </details>
                )}
              </div>

              {/* ── INC36 L1 ⑤「内联 Artifact chip」——自然语言结果里的产物（新 testid）── */}
              {real && <InlineArtifacts runId={real.run_id} artifacts={artifacts} />}

              {/* ── INC36 / INC39 L1 ⑥「底部 follow-up」——**统一续聊入口**（ChatGPT 的
                  composer 位；真调 POST /workspace/tasks，带 parent_run_id + 主产物指纹）。
                  段⑤ 的 result-continue 已退役，对话续聊统一到这里。 ────────────────── */}
              {real && (
                <FollowUpComposer
                  runId={real.run_id}
                  onContinue={onFollowUp}
                  pending={create.isPending}
                  error={continueError}
                />
              )}
            </>
          )}
        </main>

        {/* 右列 —— 产物：产物卡 + 逐字预览 + 下载 / 诚实空态（新增 ArtifactPanel）。 */}
        <aside
          className="workspace-col workspace-col-artifacts"
          data-testid="workspace-col-artifacts"
        >
          <ArtifactPanel runId={real?.run_id ?? ''} artifacts={artifacts} />
        </aside>
      </div>
    </section>
  )
}

function RunHeader({
  title,
  meta,
  status,
  mode,
  onMode,
  llm,
}: {
  title: string
  meta: string
  status: { label: string; tone: string }
  mode: ViewMode
  onMode: (mode: ViewMode) => void
  /** INC35 —— 本次运行的模型身份（`GET /runs/{id}`.llm）；只读展示，不做切换。 */
  llm?: RunLLM | null
}) {
  return (
    <header className="runs-head">
      <div className="row">
        <div>
          <div className="title-row">
            <h1>{title}</h1>
            <span className={`badge ${status.tone}`.trim()}>
              <span
                className="dot"
                style={{ background: status.tone ? `var(--${status.tone}-4)` : 'var(--fg-muted)' }}
              />{' '}
              {status.label}
            </span>
          </div>
          <p className="sub">{meta}</p>
        </div>
        <div className="actions">
          {/* INC36 —— 顶部降噪：把「模型只读入口 + 视图模式」折进 `[⋯]`，让头部最多
              「标题 + 状态徽标 + ⋯」。`model-status` testid **不删**（展开 `[⋯]` 后仍可达）；
              状态徽标 `.runs-head .badge` **留在可见区**（inc32 ⑥），不折进 `[⋯]`。 */}
          <details className="runs-more">
            <summary className="runs-more-btn" aria-label="更多">
              ⋯
            </summary>
            <div className="runs-more-menu">
              <ModelStatus llm={llm} mode={mode} />
              <ViewModeToggle mode={mode} onMode={onMode} />
            </div>
          </details>
        </div>
      </div>
    </header>
  )
}

/** Segmented control as a radiogroup (not a tablist) — design §6.2. */
function ViewModeToggle({ mode, onMode }: { mode: ViewMode; onMode: (mode: ViewMode) => void }) {
  const ids: ViewMode[] = ['concise', 'debug']
  const refs = useRef<Record<ViewMode, HTMLButtonElement | null>>({ concise: null, debug: null })

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return
    e.preventDefault()
    const dir = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1
    const next = ids[(ids.indexOf(mode) + dir + ids.length) % ids.length]
    onMode(next)
    refs.current[next]?.focus()
  }

  return (
    <div
      className="seg"
      role="radiogroup"
      aria-label="视图模式：简洁模式面向业务，调试模式面向开发者"
      onKeyDown={onKeyDown}
    >
      {ids.map((id) => (
        <button
          key={id}
          ref={(el) => {
            refs.current[id] = el
          }}
          type="button"
          role="radio"
          aria-checked={mode === id}
          tabIndex={mode === id ? 0 : -1}
          onClick={() => onMode(id)}
        >
          {id === 'concise' ? '简洁' : '调试'}
        </button>
      ))}
    </div>
  )
}
