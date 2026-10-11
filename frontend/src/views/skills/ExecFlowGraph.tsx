/**
 * ExecFlowGraph —— INC46 T14「执行流程图」。
 *
 * 把**技能执行过程**可视化为带连线的图：节点 = 步骤 / 验证层 / HITL 暂停点，
 * 连线 = 顺序边（`seq`）+ 分支边（`branch`，未适用步骤与 HITL 暂停点）。图模型由
 * `execFlow.ts` 的纯函数给出（**零编造**）。
 *
 * 数据来源（全部为**既有**端点，未新增任何后端 API）：
 *   · `GET /runs?limit=`        → 可选的真实运行列表（下拉选择）；
 *   · `GET /runs/{id}`          → 真实 `steps` / `plan`（真实 run_steps + 验证层 verdict）；
 *   · `GET /pending-actions`    → HITL 暂停点；
 *   · `GET /skills/{id}/engineering` → 无 run 时回落到技能真实契约（procedure/verification）。
 *
 * 交互收口（三态齐全，均有稳定 testid）：
 *   · 加载态 `exec-flow-loading`；错误态 `exec-flow-error`（role=alert）；
 *   · 空态：无运行记录 `exec-flow-no-runs`、无图数据 `exec-flow-empty`（均显式「—」）。
 *
 * 诚实纪律（§8 / 红线 12）：**无数据一律「—」**，绝不渲染伪造节点或假连线——
 * 阳性/阴性/反事实三重探针见 `frontend/e2e/inc46_t14_exec_flow.spec.ts`。
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { fetchPendingActions } from '../../api/client'
import type { PendingAction, RunSummary } from '../../api/client'
import { useRecentHubRuns, useRunDetail, useSkillEngineering } from '../../api/hooks'
import type { EngineeringSubject } from '../../api/hooks'
import type { FlowEdge, FlowGraphModel, FlowNodeKind } from './execFlow'
import { deriveRunFlow, deriveSkillFlow } from './execFlow'
import type { SkillSubject } from './skillAssets'
import '../../styles/skill-flow.css'

/** 图的渲染阶段（与数据是否存在的「空」区分开）。 */
export type FlowPhase = 'loading' | 'error' | 'ready'

const KIND_LABEL: Record<FlowNodeKind, string> = {
  step: '步骤',
  verify: '验证层',
  hitl: 'HITL',
}

/** 一个节点发出的所有连线（顺序 + 分支），按 `from` 索引。 */
function edgesFrom(model: FlowGraphModel, nodeId: string): FlowEdge[] {
  return model.edges.filter((edge) => edge.from === nodeId)
}

/**
 * INC54 §九 —— 下拉选项的时间标签（纯展示，只读后端真实 `created_at`）。
 *
 * 今天 ⇒ `10:32`；昨天 ⇒ `昨天 18:21`；更早 ⇒ `10-06 18:21`。`created_at`
 * **不可解析**时返回 `''`（该段整条省略）——不猜、不填假时间。
 * 「今天 / 昨天」边界取**本地日历日**零点，与 `conversation.groupRunsByDay` 同口径。
 */
function runTimeLabel(createdAt: string): string {
  const t = Date.parse(createdAt)
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  const now = new Date()
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime()
  if (t >= startOfToday) return hhmm
  if (t >= startOfYesterday) return `昨天 ${hhmm}`
  const mmdd = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return `${mmdd} ${hhmm}`
}

/**
 * INC54 §九 —— 把不可读的裸 `run_id` 换成 `意图前 20 字 · run_id 前 8 位 · 时间`。
 *
 * 例：`修改季度报告第三部分 · a81f3c21 · 10:32`。**不删**这个下拉能力（用户明确要求
 * 保留），只是让每一行长成人类可读的样子。意图缺失 ⇒ 用「（无任务描述）」占位，
 * 绝不把 UUID 再铺回去。
 *
 * ⚠️ **不导出**（模块私有）：本文件是组件文件，导出非组件函数会触发
 * `react-refresh/only-export-components`（`make lint` 是硬门禁）。该纯函数无外部
 * 调用方（行为由 `e2e/inc54_replay_lifecycle.spec.ts` ③ 经浏览器钉住）。
 */
function runOptionLabel(run: RunSummary): string {
  const intent = (run.intent || run.title || '').trim()
  const head = intent ? (intent.length > 20 ? `${intent.slice(0, 20)}…` : intent) : '（无任务描述）'
  const shortId = (run.run_id || '').slice(0, 8)
  const when = runTimeLabel(run.created_at)
  return [head, shortId, when].filter(Boolean).join(' · ')
}

/* ------------------------------------------------------------------------- *
 * 展示层（纯）：三态 + 图。所有数据已由容器派生，这里只渲染。
 * ------------------------------------------------------------------------- */
export function ExecFlowGraph({
  model,
  phase,
  errorLabel,
}: {
  model: FlowGraphModel
  phase: FlowPhase
  errorLabel: string
}) {
  return (
    <div className="skill-flow-body" data-testid="exec-flow-graph" data-phase={phase}>
      {phase === 'loading' ? (
        <p className="skill-flow-loading" data-testid="exec-flow-loading">
          执行流程图加载中…
        </p>
      ) : phase === 'error' ? (
        <p className="skill-flow-error" data-testid="exec-flow-error" role="alert">
          {errorLabel}
        </p>
      ) : model.hasData ? (
        <ol className="skill-flow-spine" aria-label="执行流程图节点">
          {model.nodes.map((node) => (
            <li key={node.id} className="skill-flow-item">
              <div
                className="skill-flow-node"
                data-testid="exec-flow-node"
                data-kind={node.kind}
                data-status={node.status}
                data-node-id={node.id}
              >
                <span className="skill-flow-node-kind">{KIND_LABEL[node.kind]}</span>
                <span className="skill-flow-node-label">{node.label}</span>
                {node.detail ? (
                  <span className="skill-flow-node-detail">{node.detail}</span>
                ) : null}
              </div>
              {edgesFrom(model, node.id).map((edge) => (
                <div
                  key={edge.id}
                  className="skill-flow-edge"
                  data-testid="exec-flow-edge"
                  data-kind={edge.kind}
                  data-from={edge.from}
                  data-to={edge.to}
                >
                  <span aria-hidden="true">{edge.kind === 'branch' ? '↳' : '↓'}</span>
                </div>
              ))}
            </li>
          ))}
        </ol>
      ) : (
        <p className="skill-flow-empty" data-testid="exec-flow-empty">
          —
        </p>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------------- *
 * 容器：真实 run 列表 → 选中 run 的明细 + HITL 暂停点 → 图模型。
 * ------------------------------------------------------------------------- */

/** run_id 可空 ⇒ 不请求（避免为「未选中」发一次注定 404 的请求）。 */
function usePendingActions(runId: string | null) {
  return useQuery({
    queryKey: ['pending-actions', runId],
    queryFn: () => fetchPendingActions(runId as string),
    enabled: !!runId,
  })
}

function engineeringSubjectOf(subject: SkillSubject | null): EngineeringSubject | null {
  if (!subject) return null
  return subject.kind === 'skill'
    ? { kind: 'skill', id: subject.skill.id }
    : { kind: 'candidate', id: subject.candidate.id }
}

export function SkillExecFlowSection({ subject }: { subject: SkillSubject | null }) {
  const runsQ = useRecentHubRuns(20)
  const runs = runsQ.data?.items ?? []
  const [picked, setPicked] = useState<string | null>(null)
  const runId = picked ?? runs[0]?.run_id ?? null

  const detailQ = useRunDetail(runId)
  const pendingQ = usePendingActions(runId)
  const engQ = useSkillEngineering(engineeringSubjectOf(subject))

  const runFlow = deriveRunFlow(detailQ.data, pendingQ.data as PendingAction[] | undefined)
  const skillFlow = deriveSkillFlow(engQ.data)
  // 优先真实 run 数据；无 run 图数据时回落到技能真实契约；都没有 ⇒ 空图（「—」）。
  const model = runFlow.hasData ? runFlow : skillFlow

  let phase: FlowPhase = 'ready'
  let errorLabel = ''
  if (runsQ.isLoading) {
    phase = 'loading'
  } else if (runsQ.isError) {
    phase = 'error'
    errorLabel = '运行列表加载失败'
  } else if (runId && detailQ.isLoading) {
    phase = 'loading'
  } else if (runId && detailQ.isError) {
    phase = 'error'
    errorLabel = '运行明细加载失败'
  }

  const hasRuns = runs.length > 0

  return (
    // INC46 T14 缺陷修复：容器曾误用 `data-testid="skill-exec-flow"`，与 T06 在
    // `SkillEngineering.tsx::SkillEngineering` 的既有节点链**同名冲突**（选中技能时
    // `getByTestId('skill-exec-flow')` 命中 2 个元素，破坏 inc43 既有断言的唯一性）。
    // 改为 T14 自有 id，T06 的 `skill-exec-flow` 一字未动（只增不改不删）。
    <div className="skill-flow" data-testid="exec-flow-section">
      <div className="skill-flow-head">
        <span className="skill-flow-title">执行流程图</span>
        {hasRuns ? (
          <select
            className="skill-flow-select"
            data-testid="exec-flow-run-select"
            value={runId ?? ''}
            onChange={(event) => setPicked(event.target.value)}
            aria-label="选择一次真实运行"
          >
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {runOptionLabel(run)}
              </option>
            ))}
          </select>
        ) : null}
      </div>

      {!hasRuns && !runsQ.isLoading && !runsQ.isError ? (
        <p className="skill-flow-empty" data-testid="exec-flow-no-runs">
          — 暂无真实运行记录
        </p>
      ) : null}

      {/* HITL 暂停点取数失败不吞错：如实标注（但**不**因此整图失败）。 */}
      {pendingQ.isError ? (
        <p className="skill-flow-note" data-testid="exec-flow-pending-error" role="status">
          HITL 暂停点暂不可用
        </p>
      ) : null}

      <ExecFlowGraph model={model} phase={phase} errorLabel={errorLabel} />
    </div>
  )
}
