/**
 * execFlow —— INC46 T14「执行流程图」**纯函数派生层**。
 *
 * 职责（唯一）：把**真实**数据转成可渲染的图模型（节点 + 连线）。
 *   · 数据来源（全部为既有端点，无新增）：`GET /runs/{id}` 的 `steps` / `plan`
 *     （真实 `run_steps` 与已声明计划）、步骤上的 `verification` JSONB（验证层判决，
 *     仅在**确实携带**时渲染）、`GET /pending-actions` 的 HITL 暂停点；
 *   · 技能侧：`GET /skills/{id}/engineering` 的 `contract.procedure` / `contract.verification`
 *     / `requires_approval`（完全成形的真实契约，非草案）。
 *
 * 诚实纪律（§8 / 红线 12）：
 *   · **无数据 ⇒ `EMPTY_FLOW`**（`nodes` 与 `edges` 皆空），渲染方显式显示「—」；
 *   · **绝不**在无数据时补一个占位节点、也不把「未测量」当 `blocked` / `ok`；
 *   · 未知状态一律 `'unknown'`（中性），不做猜测映射。
 *
 * 本文件只 `import type`（运行期零依赖），与 `skillAssets.ts` 同构，便于静态审阅。
 */

import type { PendingAction, RunDetail, SkillEngineeringResponse } from '../../api/client'

/** 节点类别：步骤 / 验证层 / HITL 暂停点。 */
export type FlowNodeKind = 'step' | 'verify' | 'hitl'

/** 节点状态：只表达后端**真实**给出的六态口径；未知 ⇒ `'unknown'`。 */
export type FlowNodeStatus = 'ok' | 'error' | 'blocked' | 'pending' | 'unknown'

export type FlowNode = {
  id: string
  kind: FlowNodeKind
  label: string
  status: FlowNodeStatus
  /** 逐字来自后端的补充说明（无 ⇒ 空串，渲染方不补文案）。 */
  detail: string
}

/** 连线：`seq` = 顺序边，`branch` = 分支边（未适用 / HITL）。 */
export type FlowEdge = { id: string; from: string; to: string; kind: 'seq' | 'branch' }

export type FlowGraphModel = { nodes: FlowNode[]; edges: FlowEdge[]; hasData: boolean }

/** 无数据图模型：稳定的空值，供渲染方判 `hasData === false` ⇒ 显示「—」。 */
export const EMPTY_FLOW: FlowGraphModel = { nodes: [], edges: [], hasData: false }

/* ── 状态映射（后端六态 → 图的四态；未知不猜） ─────────────────────────────── */

function flowStatus(raw: unknown): FlowNodeStatus {
  const s = typeof raw === 'string' ? raw.toLowerCase() : ''
  switch (s) {
    case 'ok':
    case 'success':
    case 'done':
    case 'pass':
    case 'passed':
      return 'ok'
    case 'error':
    case 'failed':
    case 'failure':
    case 'fail':
    case 'unavailable':
    case 'refused':
      return 'error'
    // INC15 —— `blocked`（缺输入，未执行且**非**失败）与 `not_applicable`（未适用）。
    case 'blocked':
    case 'skipped':
    case 'not_applicable':
      return 'blocked'
    case 'running':
    case 'pending':
    case 'awaiting_approval':
    case 'paused':
    case 'pending_approval':
      return 'pending'
    default:
      return 'unknown'
  }
}

/* ── 小工具（形参可能为任意 JSON；不抛错） ─────────────────────────────────── */

function asDicts(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is Record<string, unknown> =>
      !!item && typeof item === 'object' && !Array.isArray(item),
  )
}

function firstText(source: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

/** 一个步骤节点的标签：优先真实工具名，其次步骤类型 / 备注；都没有 ⇒ 序号占位。 */
function stepLabel(step: Record<string, unknown>, index: number): string {
  return firstText(step, ['tool', 'step_type', 'note', 'name', 'title']) || `步骤 ${index + 1}`
}

/** 一个步骤节点的补充说明：逐字取 `blocked_reason` / `note`（无 ⇒ 空串）。 */
function stepDetail(step: Record<string, unknown>): string {
  return firstText(step, ['blocked_reason', 'note'])
}

/** 步骤上的验证层判决（`verification` JSONB）→ 一个 `verify` 节点；**无则无**。 */
function verifyNode(step: Record<string, unknown>, stepNodeId: string): FlowNode | null {
  const raw = step['verification']
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const v = raw as Record<string, unknown>
  let status: FlowNodeStatus
  if (v['passed'] === true || v['ok'] === true) status = 'ok'
  else if (v['passed'] === false || v['ok'] === false) status = 'error'
  else status = flowStatus(v['status'])
  const layer = firstText(v, ['layer'])
  return {
    id: `${stepNodeId}::verify`,
    kind: 'verify',
    label: layer ? `验证层 ${layer}` : '验证层',
    status,
    detail: firstText(v, ['detail', 'reason']),
  }
}

/* ── 连线：spine 顺序边 + 分支边 ───────────────────────────────────────────── */

function buildEdges(spine: string[], branchTargets: string[]): FlowEdge[] {
  const edges: FlowEdge[] = []
  for (let i = 1; i < spine.length; i += 1) {
    edges.push({ id: `e-seq-${i}`, from: spine[i - 1], to: spine[i], kind: 'seq' })
  }
  const tail = spine.length > 0 ? spine[spine.length - 1] : null
  if (tail) {
    branchTargets.forEach((to, i) => {
      edges.push({ id: `e-br-${i}`, from: tail, to, kind: 'branch' })
    })
  }
  return edges
}

/* ── 生产者 A：真实 run（`GET /runs/{id}` + `GET /pending-actions`） ───────── */

/**
 * 把一次**真实运行**的明细 + HITL 暂停点转成图模型。
 *
 * · 步骤节点：`detail.steps`（真实执行轨迹）非空即用；否则回落到 `detail.plan.steps`
 *   （已声明计划，`applicability === 'blocked'` ⇒ `blocked`，否则 `pending`）；
 * · 验证层节点：仅当某步骤**真的**携带 `verification` 对象时生成；
 * · HITL 暂停点：仅 `pending` 中**属于该 run** 的条目；
 * · 分支节点：`detail.plan.not_applicable`（真实的「未适用」候选步骤）。
 *
 * 全部为空 ⇒ `EMPTY_FLOW`（**不伪造**任何节点）。
 */
export function deriveRunFlow(
  detail: RunDetail | null | undefined,
  pending: PendingAction[] | null | undefined,
): FlowGraphModel {
  if (!detail) return EMPTY_FLOW

  const nodes: FlowNode[] = []
  const spine: string[] = []

  const executed = asDicts(detail.steps)
  const planned = asDicts(detail.plan?.steps)
  const steps = executed.length > 0 ? executed : planned

  steps.forEach((step, i) => {
    const id = `step-${i}`
    const rawStatus = step['status'] ?? step['applicability']
    nodes.push({
      id,
      kind: 'step',
      label: stepLabel(step, i),
      status: flowStatus(rawStatus),
      detail: stepDetail(step),
    })
    spine.push(id)
    const ver = verifyNode(step, id)
    if (ver) {
      nodes.push(ver)
      spine.push(ver.id)
    }
  })

  const branchTargets: string[] = []

  asDicts(detail.plan?.not_applicable).forEach((entry, i) => {
    const id = `na-${i}`
    nodes.push({
      id,
      kind: 'step',
      label: stepLabel(entry, i),
      status: 'blocked',
      detail: firstText(entry, ['reason']),
    })
    branchTargets.push(id)
  })

  const runId = typeof detail.run_id === 'string' ? detail.run_id : ''
  // 防御：端点返回非数组（老桩 / 降级响应）时按「无数据」处理，绝不抛错、也不编造。
  const pendingList = Array.isArray(pending) ? pending : []
  const hitl = pendingList.filter(
    (item) => item && typeof item.run_id === 'string' && (!runId || item.run_id === runId),
  )
  hitl.forEach((item, i) => {
    const id = `hitl-${i}`
    nodes.push({
      id,
      kind: 'hitl',
      label: `HITL · ${item.kind || '待处理'}`,
      status: item.status === 'waiting' ? 'pending' : flowStatus(item.status),
      detail: item.resolution ?? (item.expires_at ? `到期 ${item.expires_at}` : ''),
    })
    branchTargets.push(id)
  })

  if (nodes.length === 0) return EMPTY_FLOW
  return { nodes, edges: buildEdges(spine, branchTargets), hasData: true }
}

/* ── 生产者 B：技能契约（`GET /skills/{id}/engineering`） ──────────────────── */

/**
 * 把技能**真实工程契约**（完全成形的 `SkillContract`）转成图模型。
 *
 * · 步骤节点：`contract.procedure`（真实已声明过程，状态一律 `pending` =
 *   「已声明、尚未执行」，**不谎报**为 `ok`）；
 * · 验证层节点：`contract.verification`；
 * · HITL 暂停点：`requires_approval` 或 `risk_level === 'high'`（真实高风险强制 REVIEW）。
 *
 * 三者皆空 ⇒ `EMPTY_FLOW`。
 */
export function deriveSkillFlow(
  engineering: SkillEngineeringResponse | null | undefined,
): FlowGraphModel {
  const contract = engineering?.contract
  if (!contract) return EMPTY_FLOW

  const nodes: FlowNode[] = []
  const spine: string[] = []

  for (const [i, text] of (contract.procedure ?? []).entries()) {
    const id = `proc-${i}`
    nodes.push({ id, kind: 'step', label: String(text), status: 'pending', detail: '' })
    spine.push(id)
  }
  for (const [i, text] of (contract.verification ?? []).entries()) {
    const id = `verify-${i}`
    nodes.push({ id, kind: 'verify', label: String(text), status: 'unknown', detail: '' })
    spine.push(id)
  }

  const branchTargets: string[] = []
  const needsHitl = engineering?.requires_approval === true || contract.risk_level === 'high'
  if (needsHitl) {
    const id = 'hitl-0'
    nodes.push({
      id,
      kind: 'hitl',
      label: 'HITL · 发布审批',
      status: 'pending',
      detail: contract.risk_level ? `风险 ${contract.risk_level}` : '',
    })
    branchTargets.push(id)
  }

  if (nodes.length === 0) return EMPTY_FLOW
  return { nodes, edges: buildEdges(spine, branchTargets), hasData: true }
}
