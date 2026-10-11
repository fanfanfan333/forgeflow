/**
 * inspect.ts — Agent 工作台（AgentFlow 2.0 / Phase B）的**纯派生层**
 * （无 React、无副作用、无新请求）。
 *
 * 职责：把**既有真实端点**的响应推导成右栏 Inspector 所需的结构 ——
 *   · `GET /runs/{id}`                 → `RunDetail`
 *   · `GET /workspace/sessions/{id}`   → `WorkspaceSessionDetail`
 * **不发任何新请求、不新增任何后端端点**（AUDIT §14.3.1：Phase B 不需要新端点）。
 *
 * 纪律（项目红线，AUDIT §14.4；违反即返工）：
 *   1. **一事实一取数** —— 状态 / 交付态 / 耗时 / 计数一律经既有 `realRun.ts` /
 *      `chat.ts` 派生函数，本文件**绝不重写** `status.includes(...)`、
 *      `completed_at - created_at` 之类的第二套判定。
 *   2. **无数据 ⇒ `null` / 空数组**（由 UI 渲染「—」/「暂无数据」）；**严禁**用零值
 *      兜底运算符把「未测量」谎报成 0（诚实门禁硬约束，见 AUDIT §9.5）。
 *   3. **无真实来源的字段**（技能级成功率 / Run↔Skill 关联 / 数据集 Schema /
 *      一等 ChangeSet）一律留 `null`，由 UI 显式「暂无数据」，**不填假数字**。
 */
import type {
  RunDetail,
  Skill,
  SkillReadinessResponse,
  VersionChainResponse,
  WorkspaceSessionDetail,
} from '../../api/client'
import {
  deriveArtifacts,
  deriveInjectedContext,
  deliveryState,
  fmtTime,
  formatRunDuration,
  outcomeMeta,
  runStatusMeta,
  runWallClockMs,
} from '../runs/realRun'
import { invokedSkillNames, traceSummaryCounts } from './chat'
// Phase C / T-C01 —— 右栏「变更集」事实**委托** `changeSet.ts`（抽屉同一真源，
// 避免两处取数漂移；AUDIT §16.7.1）。`changeSet.ts` 只对 `./inspect` 做**类型**导入，
// 故此处无运行期循环依赖。
import { changeSetFields, deriveChangeSet } from './changeSet'

/* ------------------------------------------------------------------------- *
 * 类型（与 AUDIT §14.3.2 契约逐字一致）
 * ------------------------------------------------------------------------- */

export type InspectKind = 'run' | 'artifact' | 'skill' | 'memory' | 'changeset' | 'file'

/**
 * 右栏可检视对象（选中态）。`id` 为域内唯一键：
 * `run_id` / `artifact.id` / `skill.id` 或技能名 / `memory.id` / `'codeset'` /
 * `resource.id`。`label` 是面向用户的短标签。
 */
export type InspectObject = { kind: InspectKind | 'resource'; id: string; label: string; runId?: string }

/** 一条只读事实（缺失 ⇒ `value=null`，由 UI 渲染「—」/「暂无数据」）。 */
export type InspectFact = { key: string; label: string; value: string | null }

/** 左栏对象树（会话级）。 */
export type SessionObjectTree = {
  sessionId: string | null
  title: string
  runs: { runId: string; intent: string; status: string; createdAt: string | null }[]
  /** 最新 run 派生的对象（无 run ⇒ 空，UI 显示「暂无数据」）。 */
  objects: InspectObject[]
}

/* ------------------------------------------------------------------------- *
 * 小工具（纯）
 * ------------------------------------------------------------------------- */

/** 运行体明细是否**保留**到本进程（`detail_retained === false` ⇒ 计数不可信）。 */
function runRetained(detail: RunDetail): boolean {
  return detail.detail_retained !== false
}

/** 计数转展示串；运行体未保留 ⇒ `null`（未测量，**绝不**臆造 0）。 */
function countValue(count: number, retained: boolean): string | null {
  return retained ? String(count) : null
}

/** 非空 trim 串，否则 `null`（缺失即「—」，不造占位）。导出供 `inspectorSections.ts` 复用。 */
export function nonEmpty(value: string | null | undefined): string | null {
  const s = (value ?? '').trim()
  return s.length > 0 ? s : null
}

/** 0..1 比率 → 百分比文本；调用方保证入参非 `null`。 */
function formatRatio(ratio: number): string {
  return `${Math.round(ratio * 100)}%`
}

/** 会话标题缺省回落：首条 run 的 intent 前 40 字（对齐既有 `useChatThread.title` 口径）。 */
function firstIntentTitle(runs: SessionObjectTree['runs']): string | null {
  const first = runs.length > 0 ? runs[0] : null
  const intent = nonEmpty(first?.intent)
  if (!intent) return null
  return intent.length > 40 ? `${intent.slice(0, 40)}…` : intent
}

/* ------------------------------------------------------------------------- *
 * 右栏：Run Inspector 事实
 * ------------------------------------------------------------------------- */

/**
 * `GET /runs/{id}`（`RunDetail`）→ Run 的只读事实。
 *
 * 逐项取数（全部为既有派生函数的输出，**不新增判定**）：
 *   · 运行状态 ← `runStatusMeta(detail.status)`（唯一词表源）；
 *   · 交付状态 ← `deliveryState(detail)`（唯一终态源）；
 *   · 本次结论 ← `outcomeMeta(detail.outcome)`；
 *   · 耗时     ← `runWallClockMs(created_at, completed_at)` → `formatRunDuration`；
 *                两端缺 ⇒ **整项省略**（未测量，不写 0 / 不写「—」）；
 *   · 模型     ← `detail.llm.model`（无 ⇒「未知」，**绝不**写「已连接」）；
 *   · 步骤/工具/技能计数 ← `traceSummaryCounts(detail)`（运行体未保留 ⇒ `—`）；
 *   · Tokens / 成本 ← 后端真实数字，缺 ⇒ **整项省略**；
 *   · 错误 / 明细保留 ← `detail.errors` / `detail.detail_retained`。
 */
export function runInspectFacts(detail: RunDetail): InspectFact[] {
  const facts: InspectFact[] = [
    { key: 'status', label: '运行状态', value: runStatusMeta(detail.status).label },
    { key: 'delivery', label: '交付状态', value: deliveryState(detail).label },
    { key: 'outcome', label: '本次结论', value: outcomeMeta(detail.outcome).label },
    { key: 'intent', label: '任务诉求', value: nonEmpty(detail.intent) },
  ]

  // 耗时：仅当两端都存在且可解析时才出该项（未测量 ⇒ 整项省略）。
  const wallMs = runWallClockMs(detail.created_at, detail.completed_at)
  if (wallMs !== null) {
    facts.push({ key: 'duration', label: '耗时', value: formatRunDuration(wallMs) })
  }

  // 模型：`detail.llm` 里没有明确模型串时显示「未知」。
  const llm = detail.llm as Record<string, unknown> | undefined
  const model = llm && typeof llm.model === 'string' ? llm.model.trim() : ''
  facts.push({ key: 'model', label: '模型', value: model || '未知' })

  // 计数：运行体未保留 ⇒ 三项均「—」（未测量，不臆造 0）。
  const retained = runRetained(detail)
  const counts = traceSummaryCounts(detail)
  facts.push({ key: 'steps', label: '步骤数', value: countValue(counts.steps, retained) })
  facts.push({ key: 'tools', label: '工具数', value: countValue(counts.tools, retained) })
  facts.push({ key: 'skills', label: '技能数', value: countValue(counts.skills, retained) })

  // Tokens / 成本：仅当后端给出真实数字时出该项（缺失 ⇒ 整项省略）。
  if (typeof detail.total_tokens === 'number') {
    facts.push({ key: 'tokens', label: 'Tokens', value: detail.total_tokens.toLocaleString() })
  }
  if (typeof detail.total_cost_usd === 'number') {
    facts.push({ key: 'cost', label: '成本', value: `$${detail.total_cost_usd.toFixed(4)}` })
  }

  // 错误：后端 `errors[]` 为真实列表；运行体未保留 ⇒ 不展示（不臆造「无错误」）。
  if (retained) {
    facts.push({
      key: 'errors',
      label: '错误',
      value: detail.errors.length > 0 ? `${detail.errors.length} 项` : '无',
    })
  }
  facts.push({
    key: 'detail_retained',
    label: '运行明细',
    value: retained ? '已保留' : '未保留（重启后仅存表头）',
  })
  return facts
}

/* ------------------------------------------------------------------------- *
 * 右栏：Artifact Inspector 事实
 * ------------------------------------------------------------------------- */

/**
 * `GET /runs/{id}`.artifacts（`deriveArtifacts`）→ 指定产物的只读事实。
 *
 * 找不到该 `id` ⇒ `[]`（诚实空态，不臆造）。`format === 'docx'` 且带 `diff` 时，
 * 追加后端**逐字**给出的修改/新增/删除/数值改动计数（`DocDiff`）。
 */
export function artifactInspectFacts(detail: RunDetail, id: string): InspectFact[] {
  const artifact = deriveArtifacts(detail).find((a) => a.id === id)
  if (!artifact) return []
  const facts: InspectFact[] = [
    { key: 'title', label: '名称', value: nonEmpty(artifact.title) },
    { key: 'kind', label: '类型', value: nonEmpty(artifact.kind) },
    { key: 'format', label: '格式', value: nonEmpty(artifact.format) },
    { key: 'source', label: '产出工具', value: nonEmpty(artifact.source) },
    { key: 'created', label: '生成时间', value: fmtTime(artifact.created_at) },
    { key: 'chars', label: '正文字符数', value: artifact.content.length.toLocaleString() },
    { key: 'ref', label: '内容摘要', value: nonEmpty(artifact.result_ref) },
  ]
  if (artifact.diff) {
    const d = artifact.diff
    facts.push(
      { key: 'diff_modified', label: '修改处数', value: String(d.modified) },
      { key: 'diff_added', label: '新增行数', value: String(d.added) },
      { key: 'diff_removed', label: '删除行数', value: String(d.removed) },
      { key: 'diff_numeric', label: '数值改动', value: String(d.numeric_changes) },
    )
  }
  return facts
}

/* ------------------------------------------------------------------------- *
 * 右栏：Skill Inspector 事实
 * ------------------------------------------------------------------------- */

/**
 * `GET /skills`（`Skill`）+ `GET /skills/{id}/readiness` → Skill 的只读事实。
 *
 * 诚实边界（AUDIT G-03 / G-04 / G-06）：
 *   · `usage_count` 是后端**静态列**（featured seeds 硬编码 `bump_usage` 无调用方），
 *     故如实标注「（内置常量，未接线）」，避免被误读为动态使用统计；
 *   · `readiness.rate` 未测量 ⇒ `null`（UI 渲染「—」，**绝不**写 0）；
 *   · **成功率** / **最近运行** 当前**无真实来源**（技能级统计端点属 Phase G、
 *     Run↔Skill 关联属 Phase F）⇒ 值恒 `null`，由 UI 显示「暂无数据」，**不填假数字**。
 */
export function skillInspectFacts(
  skill: Skill,
  readiness?: SkillReadinessResponse | null,
): InspectFact[] {
  const facts: InspectFact[] = [
    { key: 'name', label: '名称', value: nonEmpty(skill.name) },
    { key: 'domain', label: '领域', value: nonEmpty(skill.domain) },
    { key: 'owner', label: '负责人', value: nonEmpty(skill.owner) },
    { key: 'version', label: '当前版本', value: nonEmpty(skill.current_version) },
    { key: 'status', label: '状态', value: nonEmpty(skill.status) },
    { key: 'description', label: '说明', value: nonEmpty(skill.description) },
    { key: 'usage', label: '使用次数', value: `${skill.usage_count}（内置常量，未接线）` },
  ]
  if (readiness) {
    facts.push({ key: 'ready', label: '就绪', value: readiness.ready ? '已就绪' : '未就绪' })
    facts.push({
      key: 'rate',
      label: '质量比率',
      value: readiness.rate === null ? null : formatRatio(readiness.rate),
    })
  }
  // 当前无真实来源：显式「暂无数据 + 出处」文案（**绝不**填 0 / 占位数字）。见 AUDIT G-03 / G-06。
  facts.push({ key: 'success_rate', label: '成功率', value: '暂无数据（技能级统计端点待 Phase G）' })
  facts.push({ key: 'recent_runs', label: '最近运行', value: '暂无数据（Run↔Skill 关联待 Phase F）' })
  return facts
}

/* ------------------------------------------------------------------------- *
 * 右栏：ChangeSet Inspector 事实（只读投影，非一等对象 —— Phase C 建）
 * ------------------------------------------------------------------------- */

/**
 * `GET /runs/{id}`(.codeplane) [+ `GET /artifacts/{id}/versions`] → 变更集的只读事实。
 *
 * **单一真源**（Phase C / T-C01 + T-C03）：本函数**委托** `changeSet.ts`
 * （`deriveChangeSet` → `changeSetFields`），与工作台 `ChangeSetDrawer` 用**同一**
 * 派生对象取数 —— 不再有本文件自己 `reduce` 加/删行的第二处取数（AUDIT §16.7.1）。
 *
 * 代码面优先（`codeplane` 有证据）；否则文档面（run 有文档类 artifact，需 `chain`）。
 * 两面皆无 ⇒ `[]`（UI 显示「本次无代码变更」）。`chain` 缺省 `null` ⇒ 文档面诚实空态。
 */
export function changeSetInspectFacts(
  detail: RunDetail,
  chain: VersionChainResponse | null = null,
): InspectFact[] {
  const changeSet = deriveChangeSet(detail, chain)
  if (!changeSet) return []
  return changeSetFields(changeSet)
}

/* ------------------------------------------------------------------------- *
 * 左栏：对象树
 * ------------------------------------------------------------------------- */

/**
 * 一次运行 → 可检视对象清单（左栏对象树的中栏投影）。
 *
 * 来源（皆为 `GET /runs/{id}` 的真实字段经既有派生函数，**零新增请求**）：
 *   · Run    ← `detail.run_id` / `intent`；
 *   · Artifact← `deriveArtifacts(detail)`；
 *   · Skill  ← `deriveInjectedContext`（带真实 `id`）+ `invokedSkillNames`（去重）；
 *   · Memory ← `deriveInjectedContext(detail).memory`（id 级）；
 *   · ChangeSet（代码面 **或** 文档面）← `changeSet.ts::deriveChangeSet(detail)`：
 *     有代码证据 ⇒ `'codeset'`/「代码变更集」；否则有文档类 artifact ⇒ `'docset'`/
 *     「文档变更集」（T-C03）。两面皆无 ⇒ 不产出（左栏「本次无代码变更」）。
 *
 * 无 detail ⇒ `[]`。**绝不**用租户资源库冒充「本次涉及」（数据集 Schema 无源，见
 * AUDIT G-08）。
 */
export function deriveInspectableObjects(detail: RunDetail | null | undefined): InspectObject[] {
  if (!detail) return []
  const objects: InspectObject[] = [
    { kind: 'run', id: detail.run_id, label: nonEmpty(detail.intent) ?? detail.run_id },
  ]
  const inputs = detail.declared_inputs?.resources
  if (Array.isArray(inputs)) {
    for (const id of inputs) {
      if (typeof id === 'string') objects.push({ kind: 'resource', id, label: '上传的文件 / 数据', runId: detail.run_id })
    }
  }

  for (const artifact of deriveArtifacts(detail)) {
    objects.push({ kind: 'artifact', id: artifact.id, label: nonEmpty(artifact.title) ?? artifact.id, runId: detail.run_id })
  }

  const seenSkills = new Set<string>()
  for (const skill of deriveInjectedContext(detail).skills) {
    const id = (skill.id || skill.name || '').trim()
    if (!id || seenSkills.has(id)) continue
    seenSkills.add(id)
    const label = (skill.name || skill.id).trim()
    if (label) seenSkills.add(label)
    objects.push({ kind: 'skill', id, label: label || id })
  }
  for (const name of invokedSkillNames(detail)) {
    if (seenSkills.has(name)) continue
    seenSkills.add(name)
    objects.push({ kind: 'skill', id: name, label: name })
  }

  for (const memory of deriveInjectedContext(detail).memory) {
    objects.push({
      kind: 'memory',
      id: memory.id,
      label: memory.scope ? `${memory.scope} · ${memory.id}` : memory.id,
    })
  }

  // ChangeSet（代码面优先 → 否则文档面）：与抽屉 / 右栏用**同一** `deriveChangeSet`，
  // 保证「对象是否出现」与「抽屉呈现什么」同一真源（AUDIT §16.7.1）。此处不取版本链
  // （左栏只决定入口是否可达）；文档面的 `versionRegistered` 由抽屉按链如实呈现。
  const changeSet = deriveChangeSet(detail, null)
  if (changeSet?.source === 'code') {
    objects.push({ kind: 'changeset', id: 'codeset', label: '代码变更集' })
  } else if (changeSet?.source === 'document') {
    objects.push({ kind: 'changeset', id: 'docset', label: '文档变更集' })
  }
  return objects
}

/**
 * `GET /workspace/sessions/{id}`（会话 + run 列表）与最新 `RunDetail` →
 * 左栏对象树。无会话 ⇒ `sessionId=null` / `objects=[]`（UI 显示诚实空态）。
 */
export function deriveSessionObjectTree(
  sessionResp: WorkspaceSessionDetail | null | undefined,
  latestDetail: RunDetail | null | undefined,
): SessionObjectTree {
  const runs = (sessionResp?.runs ?? []).map((run) => ({
    runId: run.run_id,
    intent: run.intent,
    status: run.status,
    createdAt: run.created_at,
  }))
  return {
    sessionId: sessionResp?.session_id ?? null,
    title: nonEmpty(sessionResp?.title) ?? firstIntentTitle(runs) ?? '新对话',
    runs,
    objects: deriveInspectableObjects(latestDetail ?? null),
  }
}
