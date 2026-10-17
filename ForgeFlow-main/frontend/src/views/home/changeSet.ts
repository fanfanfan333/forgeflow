/**
 * changeSet.ts — AgentFlow 2.0 / Phase C「Change Set」的**纯派生层**
 * （无 React、无副作用、**不发任何新请求**）。
 *
 * 职责（AUDIT §16）：把**既有** codeplane / artifact_review 事实归一成一个一等、只读的
 * `ChangeSet`，供工作台 `ChangeSetDrawer` 呈现。**代码面 + 文档面统一为一个对象**
 * （`source` 标注来源面）；代码面（T-C01 / T-C02）与文档面（T-C03，`source='document'`）
 * **同在本文件**，共用同一 `changeset.ts` 派生层，**不另立第二套**。
 *
 * 文档面真源（§16.13 / Q-C2）：run 的文档 artifact（`deriveArtifacts`，`content_ref`
 * 命中）→ `GET /artifacts/{id}/versions`（版本链，复用既有端点）→ 单文件 + 版本链摘要；
 * `versionRegistered` 仅当该 run 在版本链中确有记录才为 `true`（**不臆造**）。
 *
 * 硬约束（AUDIT §16.2.2 / §16.7，违反即返工）：
 *   1. **一事实一取数** —— diff / tests / approval 一律经既有 `realRun.ts`
 *      （`deriveCodeDiff` / `deriveCodePlane` / `deriveTestResult` / `deriveCodeApproval`），
 *      本文件**绝不**重解析 diff 文本、**绝不**重算 `+/-`、**绝不**重判 `committed`。
 *   2. **零 `?? 0` / `|| 0`**（诚实门禁硬约束）—— 无真源 ⇒ `null` / 空数组，由 UI 显
 *      「—」/「未测量」/「未记录」，**绝不**用 0 冒充。
 *   3. **无真源字段显式诚实**（§16.2.3）：修改原因（代码面无独立真源）⇒ `null`；
 *      validation 的 type / build（无端点）⇒ 恒 `'unavailable'`；风险等级（codeplane
 *      不带 `risk_level`，真源在 `GET /approvals` 的 `ApprovalRecord`）⇒ `null`。
 *   4. **不落库、不建表、不建端点**（§16.4）—— 纯派生。
 */
import type { ArtifactVersionRecord, RunArtifact, RunDetail, VersionChainResponse } from '../../api/client'
import {
  deriveArtifacts,
  deriveCodeApproval,
  deriveCodeDiff,
  deriveCodePlane,
  deriveTestResult,
} from '../runs/realRun'
import type { CodeTestResult } from '../runs/types'
import type { InspectFact } from './inspect'

/* ------------------------------------------------------------------------- *
 * 对象契约（AUDIT §16.2.1 逐字）
 * ------------------------------------------------------------------------- */

/** 来源面：代码修改（codeplane）或文件/文档修改（artifact_review，T-C03）。 */
export type ChangeSetSource = 'code' | 'document'

/** 单个被改文件（代码面 = unified diff 的逐文件统计；文档面 = 单文件）。 */
export type FileChange = { path: string; additions: number; deletions: number }

/** validation 结论（**只**记录真跑过的工具；无端点 ⇒ unavailable）。 */
export type ChangeSetValidation = {
  /** 真跑过的校验工具名，逐字（如 `code.lint`）；未跑 ⇒ 空数组。 */
  ran: string[]
  /** 文件语法 / type / build：后端**无编排端点** ⇒ 恒 `'unavailable'`。 */
  syntax: 'available' | 'unavailable'
  typecheck: 'available' | 'unavailable'
  build: 'available' | 'unavailable'
  note: string | null
}

/** 一等「变更集」——代码面 + 文档面归一后的**只读投影**（不落库）。 */
export type ChangeSet = {
  /** #1 变更集 ID（代码面 = `run_id`）。 */
  id: string
  /** 面：code | document。 */
  source: ChangeSetSource
  /** 关联 Run（§2.4 硬要求）。 */
  runId: string
  /** #2 用户任务（`RunRecord.intent`，逐字）。 */
  task: string
  /** #3 修改原因（无真源 ⇒ `null`）。 */
  reason: string | null
  /** #4 修改文件列表。 */
  files: FileChange[]
  /** #5/#6 before-after / unified diff（逐字）。 */
  diffText: string
  /** 是否有真实变更文本。 */
  diffPresent: boolean
  /** #7 validation result。 */
  validation: ChangeSetValidation | null
  /** #8 test result（复用既有类型；文档面不适用 ⇒ `null`）。 */
  test: CodeTestResult | null
  /** #9 风险等级（无真源 ⇒ `null`）。 */
  riskLevel: string | null
  /** #10 是否需要 HITL。 */
  needsHitl: boolean
  /** #11 用户是否已批准（`null` = 未决）。 */
  approved: boolean | null
  /** 审批状态词表（逐字，复用 `CodeApproval.status`，**不新增第二套**）。 */
  approvalStatus: string
  /** #12 最终是否应用（`codeplane.committed`）。 */
  applied: boolean
  /**
   * §16.13(4) —— 该 run + artifact 是否**确有**版本链记录（`true` 仅当链上存在
   * `run_id === 本 run` 的版本）。**代码面恒 `false`**（代码面的真源是 `applied`，
   * 不在 artifact 版本链里）；**文档面**链拿不到 ⇒ `false`（显诚实空态，非「通过」）。
   */
  versionRegistered: boolean
  /**
   * §16.13(4) —— 文档面版本登记失败时的**真实**异常摘要（run payload 的
   * `artifact_review_error` 透传）；无失败 ⇒ `null`。用于该文件行显示「版本未登记」
   * + 真实原因，**绝不用「无变更」糊过去**。
   */
  artifactReviewError: string | null
}

/* ------------------------------------------------------------------------- *
 * 代码面派生
 * ------------------------------------------------------------------------- */

/**
 * 代码面「真跑过的校验工具」白名单。
 *
 * 依据 AUDIT §16.2.2 #7：validation 的 `ran` **只**记「真跑过的校验工具名」。本仓库
 * 现有可编排的独立校验工具是 `code.lint`（`git.diff` 产出 diff 而非校验、`code.run`
 * 只做 AST 校验且默认离线档被移除）。此处**白名单式**收录，未知工具**不**臆造为校验。
 */
const VALIDATION_TOOLS: ReadonlySet<string> = new Set(['code.lint'])

/** 一次 codeplane.validation 的只读派生（全部来自真实字段）。 */
function codeValidation(detail: RunDetail, tests: CodeTestResult): ChangeSetValidation {
  const ran = Array.from(
    new Set(
      (detail.tool_invocations ?? [])
        .filter(
          (inv) =>
            inv.status === 'ok' &&
            inv.executed === true &&
            typeof inv.tool === 'string' &&
            VALIDATION_TOOLS.has(inv.tool),
        )
        .map((inv) => inv.tool as string),
    ),
  )
  return {
    ran,
    // type / build 无编排端点（§16.2.3）⇒ 恒 unavailable，绝不冒充「通过」。
    syntax: 'unavailable',
    typecheck: 'unavailable',
    build: 'unavailable',
    // note 只承载**真实**测试命令（`codeplane.tests.command`）；未测量 ⇒ null。
    note: tests.measured && tests.command.trim() !== '' ? `测试命令：${tests.command}` : null,
  }
}

/**
 * `GET /runs/{id}`.codeplane → 代码面 `ChangeSet`；非代码任务 ⇒ `null`（诚实空态）。
 *
 * 逐字段取数（**全部**经既有 `realRun.ts` 派生函数，本文件不新增判定）：
 *   · id / runId / task ← `detail.run_id` / `detail.intent`；
 *   · files / diffText / diffPresent ← `deriveCodeDiff`（逐文件 `path/additions/deletions`）；
 *   · test ← `deriveTestResult`（`CodeTestResult`，判定权在 ForgeFlow 侧）；
 *   · needsHitl / approved / approvalStatus / applied ← `deriveCodeApproval`；
 *   · validation ← `codeValidation`（真实 `code.lint` 调用 + 测试命令）；
 *   · reason / riskLevel ← `null`（§16.2.3：无真源，不臆造）。
 */
export function deriveCodeChangeSet(detail: RunDetail | null | undefined): ChangeSet | null {
  if (!detail) return null
  const plane = deriveCodePlane(detail)
  if (!plane.present) return null

  const diff = deriveCodeDiff(detail)
  const tests = deriveTestResult(detail)
  const approval = deriveCodeApproval(detail)

  const files: FileChange[] = diff.files.map((f) => ({
    path: f.path,
    additions: f.additions,
    deletions: f.deletions,
  }))

  const hasApprovalSignal = approval.status !== '' || approval.approvalId !== ''
  const approved =
    approval.status === 'approved' ? true : approval.status === 'rejected' ? false : null
  const riskLevel =
    typeof (detail.codeplane as Record<string, unknown> | undefined)?.risk_level === 'string'
      ? String((detail.codeplane as Record<string, unknown>).risk_level)
      : null

  return {
    id: detail.run_id,
    source: 'code',
    runId: detail.run_id,
    task: detail.intent ?? '',
    // #3 修改原因：代码面无独立真源（codeplane 无 reason 字段）⇒ 诚实 null。
    reason: null,
    files,
    diffText: diff.text,
    diffPresent: diff.present,
    validation: codeValidation(detail, tests),
    test: tests,
    riskLevel,
    needsHitl: hasApprovalSignal,
    approved,
    approvalStatus: approval.status,
    applied: approval.committed,
    // 代码面不在 artifact 版本链里（真源是 applied）；无登记失败一说 ⇒ 诚实恒定。
    versionRegistered: false,
    artifactReviewError: null,
  }
}

/* ------------------------------------------------------------------------- *
 * 文档面派生（T-C03 / §16.13）
 * ------------------------------------------------------------------------- */

/** 文档类 artifact 的 `format` 词表（`_deliverable_artifact` 的 fmt 取值）。 */
const DOC_FORMATS: ReadonlySet<string> = new Set(['docx', 'pptx', 'text', 'xlsx'])

/**
 * 本 run 的**文档类**交付物（`deriveArtifacts`，**只增不改**既有派生层）。
 *
 * 判据（§16.13 / Q-C2）：`content_ref` 非空 **且** `format ∈ {docx, pptx, text, xlsx}`。
 * `content_ref` 是版本链的 `artifact_id`（无它即无法取链）⇒ 缺 `content_ref` 的产物
 * **不**构成文档面 Change Set（诚实地「无法关联版本链」，**不臆造**）。
 */
function pickDocumentArtifact(detail: RunDetail): RunArtifact | null {
  for (const artifact of deriveArtifacts(detail)) {
    const ref = (artifact.content_ref ?? '').trim()
    const fmt = (artifact.format ?? '').trim().toLowerCase()
    if (ref !== '' && DOC_FORMATS.has(fmt)) return artifact
  }
  return null
}

/**
 * 本 run 的文档 artifact 的 `content_ref`（= 版本链 `artifact_id`，Q-C2）。
 * 无文档产物 ⇒ `null`（供 `useArtifactVersionChain` 决定是否发请求）。
 */
export function documentArtifactId(detail: RunDetail | null | undefined): string | null {
  if (!detail) return null
  const doc = pickDocumentArtifact(detail)
  if (!doc) return null
  const ref = (doc.content_ref ?? '').trim()
  return ref !== '' ? ref : null
}

/** 版本链版本行里的段级 diff 计数块（`versions[].diff.counts`）；无 ⇒ `null`。 */
function documentDiffCounts(
  version: ArtifactVersionRecord | null,
): { added?: number; removed?: number } | null {
  const diff = version?.diff
  if (!diff || typeof diff !== 'object') return null
  const counts = (diff as { counts?: unknown }).counts
  if (!counts || typeof counts !== 'object') return null
  return counts as { added?: number; removed?: number }
}

/**
 * §16.13(4) —— run payload 里**真实**的 `artifact_review_error`（版本登记失败摘要）。
 *
 * 从 run 的 `tool_invocations[].payload` 读取（handler 失败时追加）；无 ⇒ `null`。
 * 逐条扫描取**首个非空**摘要，**绝不**臆造原因。
 */
function documentArtifactReviewError(detail: RunDetail): string | null {
  for (const inv of detail.tool_invocations ?? []) {
    const payload = (inv as { payload?: unknown }).payload
    if (!payload || typeof payload !== 'object') continue
    const err = (payload as { artifact_review_error?: unknown }).artifact_review_error
    if (typeof err === 'string' && err.trim() !== '') return err.trim()
  }
  return null
}

/**
 * 文档面「版本登记」行文案（§16.13(4)，诚实三分支）：
 *   · 已登记 ⇒「已登记」；
 *   · 登记失败（payload 带真实摘要）⇒「版本未登记：<原因>」；
 *   · 无版本链记录 ⇒「今日无版本记录」（**非** 0、**非**「通过」）。
 */
export function versionRegisteredText(cs: ChangeSet): string {
  if (cs.versionRegistered) return '已登记'
  if (cs.artifactReviewError) return `版本未登记：${cs.artifactReviewError}`
  return '今日无版本记录'
}

/**
 * `GET /artifacts/{id}/versions`（版本链）+ 本 run 的文档 artifact → 文档面 `ChangeSet`。
 *
 * 触发条件（§16.13）：run 有**文档类** artifact（`pickDocumentArtifact`）；无 ⇒ `null`。
 * 逐字段取数（全部真源；链拿不到 ⇒ 诚实空态）：
 *   · id / runId / task ← `detail.run_id` / `detail.intent`；
 *   · files（单文件）← artifact `title`；additions/deletions ← 链上最新版本的
 *     `diff.counts.added/removed`（**取不到 ⇒ 0**，段级 diff 由版本链承载，**不重算**）；
 *   · needsHitl / approved / approvalStatus / applied ← 链上最新版本 `state` + `head_version`；
 *   · versionRegistered ← 链上是否存在 `run_id === 本 run` 的版本；
 *   · reason / validation / test / riskLevel ← `null`（文档面无真源，不臆造）。
 */
export function deriveDocumentChangeSet(
  detail: RunDetail | null | undefined,
  chain: VersionChainResponse | null,
): ChangeSet | null {
  if (!detail) return null
  const doc = pickDocumentArtifact(detail)
  if (!doc) return null

  const artifactId = (doc.content_ref ?? '').trim() || doc.id
  const versions: ArtifactVersionRecord[] = chain?.versions ?? []

  let latest: ArtifactVersionRecord | null = null
  for (const v of versions) {
    if (latest === null || v.version > latest.version) latest = v
  }
  const latestState = latest ? latest.state : ''
  const headVersion = chain ? chain.head_version : null
  const headCommitted =
    latest !== null && latestState === 'committed' && headVersion !== null && headVersion !== undefined
  const hasPending = versions.some((v) => v.state === 'pending')
  const versionRegistered = versions.some((v) => (v.run_id ?? '') === detail.run_id)

  const counts = documentDiffCounts(latest)
  const additions = counts && typeof counts.added === 'number' ? counts.added : 0
  const deletions = counts && typeof counts.removed === 'number' ? counts.removed : 0

  const path = (doc.title ?? '').trim() || artifactId

  return {
    id: detail.run_id,
    source: 'document',
    runId: detail.run_id,
    task: detail.intent ?? '',
    // 文档面无独立「修改原因」真源 ⇒ 诚实 null（UI 显「未记录修改原因」）。
    reason: null,
    files: [{ path, additions, deletions }],
    // 文档面无 unified 文本（段级 diff 由版本链承载）⇒ 诚实空文本 + diffPresent=false。
    diffText: '',
    diffPresent: false,
    validation: null,
    test: null,
    riskLevel: null,
    needsHitl: hasPending,
    approved: latestState === 'committed' ? true : null,
    approvalStatus: latestState,
    applied: headCommitted,
    versionRegistered,
    artifactReviewError: documentArtifactReviewError(detail),
  }
}

/**
 * 归一入口：把一次运行投影成 `ChangeSet`（**代码面优先 → 否则文档面 → 都无 ⇒ null**）。
 *
 * `chain`（`GET /artifacts/{id}/versions`）仅文档面需要；代码面 / 无文档产物 ⇒ 传 `null`
 * 即可（`versionRegistered` 保持诚实空态，不报错）。**不落库、不发新端点。**
 */
export function deriveChangeSet(
  detail: RunDetail | null | undefined,
  chain: VersionChainResponse | null = null,
): ChangeSet | null {
  const code = deriveCodeChangeSet(detail)
  if (code) return code
  return deriveDocumentChangeSet(detail, chain)
}

/** 变更集的加删汇总（头行「N files changed · +a / -d」用；无文件 ⇒ 0 计数）。 */
export function changeSetTotals(cs: ChangeSet): { files: number; additions: number; deletions: number } {
  return {
    files: cs.files.length,
    additions: cs.files.reduce((sum, f) => sum + f.additions, 0),
    deletions: cs.files.reduce((sum, f) => sum + f.deletions, 0),
  }
}

/* ------------------------------------------------------------------------- *
 * Inspector 事实（右栏「变更集」分段；`inspect.ts::changeSetInspectFacts` 委托本函数，
 * 保证与抽屉**同一真源**，避免两处取数漂移 —— AUDIT §16.7.1 / §16.3）
 * ------------------------------------------------------------------------- */

/** 一条只读事实文本；空串 ⇒ `null`（UI 渲染「—」）。 */
function factOrNull(value: string | null | undefined): string | null {
  const s = (value ?? '').trim()
  return s.length > 0 ? s : null
}

/**
 * `ChangeSet` → 右栏只读事实清单。
 *
 * 诚实边界（§16.2.3）：修改原因 / 风险等级无真源 ⇒ 显式「未记录修改原因」/「—」；
 * type / build 无端点 ⇒ 「不可验证」；test 未测量 ⇒ 「未测量」。
 */
export function changeSetFields(cs: ChangeSet): InspectFact[] {
  const totals = changeSetTotals(cs)
  const doc = cs.source === 'document'
  const facts: InspectFact[] = [
    { key: 'id', label: '变更集', value: `#${cs.id}` },
    { key: 'task', label: '用户任务', value: factOrNull(cs.task) },
    { key: 'source', label: '来源面', value: cs.source === 'code' ? '代码' : '文件 / 文档' },
    {
      key: 'reason',
      label: '修改原因',
      // #3 无真源 ⇒ 显式诚实（不臆造）。文档面同样无独立真源。
      value: cs.reason ?? '未记录修改原因',
    },
  ]
  if (doc) {
    // 文档面：单文件，「行数」非其口径（段级 diff 见版本链）⇒ 只显文件数。
    facts.push({
      key: 'files',
      label: '变更文件数',
      value: cs.files.length > 0 ? String(totals.files) : null,
    })
  } else {
    facts.push({ key: 'files', label: '变更文件数', value: cs.diffPresent ? String(totals.files) : null })
    facts.push({ key: 'additions', label: '新增行', value: cs.diffPresent ? String(totals.additions) : null })
    facts.push({ key: 'deletions', label: '删除行', value: cs.diffPresent ? String(totals.deletions) : null })
  }
  if (doc) {
    // §16.2.3：文档面无 validation 概念 ⇒ 显式「不适用（文档面）」。
    facts.push({ key: 'validation', label: 'validation', value: '不适用（文档面）' })
  } else if (cs.validation) {
    facts.push({
      key: 'validation_ran',
      label: '已跑校验',
      value: cs.validation.ran.length > 0 ? cs.validation.ran.join('、') : '未跑校验工具',
    })
    facts.push({
      key: 'validation_types',
      label: '语法 / 类型 / 构建',
      // 无端点 ⇒ 恒不可验证（§16.2.3）。
      value: '不可验证（无 validation 端点）',
    })
  } else {
    facts.push({ key: 'validation', label: 'validation', value: '不可验证（无 validation 机制）' })
  }
  facts.push({
    key: 'test',
    label: '测试结论',
    // 文档面不适用（非代码）；代码面无测量 ⇒ 「未测量」。
    value: doc ? '不适用（文档面）' : cs.test ? (cs.test.measured ? cs.test.verdict : '未测量') : '不适用（非代码）',
  })
  facts.push({
    key: 'risk',
    label: '风险等级',
    // codeplane 不带 risk_level（真源在 GET /approvals）⇒ 诚实「—」；文档面无 ⇒ 不适用。
    value: doc ? '不适用（文档面）' : cs.riskLevel ?? '—',
  })
  facts.push({ key: 'hitl', label: '是否需要 HITL', value: cs.needsHitl ? '需要' : '否' })
  facts.push({
    key: 'approval',
    label: '审批状态',
    value: cs.approvalStatus !== '' ? cs.approvalStatus : '未决',
  })
  facts.push({ key: 'applied', label: '是否已应用', value: cs.applied ? '已应用' : '未应用' })
  if (doc) {
    // §16.13(4) —— 版本登记行（诚实三分支，绝不臆造「已登记」）。
    facts.push({ key: 'version_registered', label: '版本登记', value: versionRegisteredText(cs) })
  }
  return facts
}
