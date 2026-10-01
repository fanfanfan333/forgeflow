/**
 * Adapter: a REAL hub run (`GET /runs/{id}`) → the `RunStage[]` that the
 * approved stage cards render.
 *
 * Why this exists: the /tasks page shipped rendering a fixed demo run, on the
 * strength of a comment claiming the backend "exposes no current-run +
 * full-timeline endpoint yet". That is false — `GET /runs` lists runs and
 * `GET /runs/{id}` returns the run's real `steps`, `errors` and
 * `tool_invocations`. This module lets the same UI render real data instead.
 *
 * Deliberately dumb: every user-visible string comes from the backend payload.
 * We do NOT invent a Chinese vocabulary for tool ids — an unknown tool is shown
 * verbatim rather than dressed up in a label nobody can trace back.
 */
import type { Experience, RunArtifact, RunDetail } from '../../api/client'
import { isModelDriven, roleForTool, stageNameForTool } from './roles'
import type {
  CodeApproval,
  CodeDiff,
  CodeDiffFile,
  CodeInjectedContext,
  CodePlaneView,
  CodeTaskSummary,
  CodeTestResult,
  CodeTimelineItem,
  DeliveryState,
  RunConclusion,
  RunEvidence,
  RunExperienceItem,
  RunMetric,
  RunRoundDetail,
  RunSection,
  RunSourceRef,
  RunStage,
  RunStageStatus,
} from './types'

/**
 * Map the runtime's six-state step contract (INC15) onto the card's display
 * states.
 *
 * `blocked` is deliberately NOT a failure — the runtime records it as "needed
 * but a required input is missing" (not executed, and not a failure), so it maps
 * to 受阻. `not_applicable` means the step did not apply to this task at all and
 * maps to 未适用. A legacy `skipped` payload is read through the same
 * `skipped → blocked` alias the backend uses, so a historical run still renders
 * 受阻 rather than a fabricated 失败 or a stale 待执行.
 */
export function stepStatusToStageStatus(status: string | undefined): RunStageStatus {
  switch ((status ?? '').toLowerCase()) {
    case 'ok':
    case 'success':
    case 'done':
      return 'done'
    case 'running':
    case 'in_progress':
    case 'started':
      return 'running'
    case 'awaiting_approval':
    case 'paused':
    case 'pending_approval':
      return 'paused'
    case 'error':
    case 'failed':
    case 'failure':
    case 'unavailable':
    case 'refused':
      return 'failed'
    // INC15 — a declared step whose required input is missing (not a failure).
    // `skipped` is the retired writer status; the reader aliases it here too so
    // an old trail is never mis-rendered.
    case 'blocked':
    case 'skipped':
      return 'blocked'
    // INC15 — a candidate step that does not apply to this task.
    case 'not_applicable':
      return 'na'
    default:
      // Any status this build does not know yet.
      return 'pending'
  }
}

/** True when the runtime says the handler actually ran for this step. */
function didExecute(status: string | undefined, executed: unknown): boolean {
  if (typeof executed === 'boolean') return executed
  // Older payloads (pre-INC12 A1) carry no `executed` flag; fall back to the
  // step's own status rather than guessing.
  const s = (status ?? '').toLowerCase()
  return ['ok', 'success', 'done', 'error', 'failed'].includes(s)
}

/**
 * A REAL per-invocation wall time, in milliseconds — or `null` when it was
 * never measured.
 *
 * ``tool_executor._elapsed_ms`` samples ``perf_counter`` around every handler
 * call and keeps sub-millisecond precision (``round(…, 3)``), so a real run
 * genuinely carries a per-tool duration (e.g. ``0.062``). We surface that same
 * measurement verbatim and ONLY when it is a positive finite number; a missing
 * field or an explicit ``null`` (blocked / unavailable / refused — never
 * measured) yields ``null``, which the card renders as 「—」. We NEVER coerce it
 * to ``0``: that would claim "measured, took no time", the exact fabrication
 * this increment removes.
 */
export function measuredMs(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

/**
 * Turn a real run into renderable stages. One stage per recorded step, in the
 * order the runtime recorded them.
 */
export function detailToStages(detail: RunDetail): RunStage[] {
  const invocations = detail.tool_invocations ?? []
  const steps = detail.steps ?? []
  // INC17 — the ReAct per-round trace (non-four-layer). Absent on pre-INC17 runs,
  // so `rounds` degrades to `[]` and the cards render exactly as before.
  const rounds = detail.llm?.rounds ?? []

  return steps.map((step, i) => {
    const index = typeof step.index === 'number' ? step.index : i
    // `step_id` is `"{run_id}:{attempt}:{index}"`, so matching on the trailing
    // `:index` stays correct across replans (each attempt has its own trail).
    const inv = invocations.find(
      (v) => typeof v.step_id === 'string' && v.step_id.endsWith(`:${index}`),
    )

    const tool = step.tool ?? (typeof inv?.tool === 'string' ? inv.tool : '')
    const status = step.status ?? (typeof inv?.status === 'string' ? inv.status : undefined)
    const note = (step.note ?? '').trim()
    const ran = didExecute(status, inv?.executed)
    // The runtime's own measured wall time for this step's tool call; 0 (no
    // measurement) is rendered as 「—」 by the card, never as 「0 ms」.
    const ms = measuredMs(inv?.latency_ms)

    // INC17 — attach the matching ReAct round (args / result snippet / model text).
    // The rounds are the model-issued tool calls in order, aligning with the
    // leading steps; we only attach when the round really names this step's tool,
    // so a mismatched / partial payload never mislabels a card.
    const roundDetail = roundFor(rounds[i], tool)
    // 本次调用**真实产出**的摘要（后端 `tool_executor.py::ToolInvocation.to_dict()` 的 `summary`，
    // 如「检索到 N 条结果」）——逐字，不构造模板。
    const invSummary = (inv as Record<string, unknown> | undefined)?.summary

    return {
      id: step.step_id ?? `${detail.run_id}-step-${index}`,
      order: index + 1,
      // INC20 / T03 —— 折叠头 `name` 是**主标识**：角色展示名 / 后端 note 原文 /
      // 真实工具 id（取值链见 `roles.stageNameForTool`），**绝不再**是 `步骤 N`
      // （AC-2：主标识 ≠ `^步骤 \d+$`）。顺序位由独立字段 `order` 承载（卡片左侧圆点）。
      name: stageNameForTool(tool, note),
      status: stepStatusToStageStatus(status),
      // INC20 / T03 —— 产出摘要优先取该次调用的**真实 summary**，回落 ReAct 轮的
      // result_snippet，再回落后端 `note`，最后才是既有诚实空态（不编计数）。
      summary: deriveStageSummary(note, invSummary, roundDetail?.resultSnippet),
      // 支撑细节（默认折叠）：保留**原始工具 id**，供追溯（不当主标识用）。
      agent: tool || step.step_type || '—',
      agentInitials: '',
      agentTone: 'muted',
      // This step's matched tool call was metered by the runtime
      // (``tool_executor._elapsed_ms``); when the measurement is missing or
      // truncated to 0 it renders as 「—」, not as a fabricated 「0 s」.
      durationMs: ms,
      sources: [],
      memories: [],
      round: roundDetail,
      // Only surface a tool row when the handler actually ran. A `skipped` step
      // has nothing to report, and the card's row styling has no neutral state —
      // an `ok:false` row would read as a failure that never happened.
      tools:
        tool && ran
          ? [
              {
                name: tool,
                // Raw id, not an invented label. `ms` is this call's real,
                // measured wall time (0 → 「—」).
                bizLabel: tool,
                agent: tool,
                ok: status === 'ok' || status === 'success' || status === 'done',
                ms,
                note: status,
              },
            ]
          : [],
    }
  })
}

/**
 * INC20 / T03 —— 工作链节点的**产出摘要**（一句「这一步产出了什么」）。
 *
 * 取数优先级（严格按序，取到即用；**全部为后端真实字段，逐字**）：
 *   1. `tool_invocations[i].summary`（`tool_executor.py::ToolInvocation.to_dict()` 写入，如
 *      「检索到 N 条结果」）——本次调用**真实产出**的摘要，**首选**；
 *   2. `llm.rounds[i].result_snippet`（ReAct 档，`react_executor.py` 中 `round_meta["result_snippet"] = feedback[:200]` 处）；
 *   3. `steps[i].note`（`orchestrator.py::_DEFAULT_STEPS`）——短文摘要回落。
 *
 * 取不到 ⇒ 既有诚实空态「（该步未记录结果说明）」（本文件 `deriveStageSummary` 的回落原文）。
 *
 * ⚠️ 不编造（P0-4 / AC-4）：**不构造模板计数句**（`payload.count` 不单独拼成
 * 「找到 N 条资料」之类），因为 AC-4 会做假计数正则扫描，自造模板句是最大假阳性来源，
 * 且 `research.search` 的 `inv.summary` 已逐字含「检索到 N 条结果」。**绝不**显示
 * 「找到 0 条」——`0` 只在后端确有一个计数字段且其值为 0 时才可渲染。
 */
export function deriveStageSummary(
  note: string,
  invSummary: unknown,
  roundSnippet: string | undefined,
): string {
  const fromInv = typeof invSummary === 'string' ? invSummary.trim() : ''
  if (fromInv) return fromInv
  const fromRound = (roundSnippet ?? '').trim()
  if (fromRound) return fromRound
  const fromNote = (note ?? '').trim()
  if (fromNote) return fromNote
  return '（该步未记录结果说明）'
}

/**
 * INC17 — coerce one `detail.llm.rounds[i]` into a renderable round detail.
 *
 * Returns `undefined` (⇒ the card renders no round block) unless the round really
 * names `tool`, so a partial / reordered payload can never attach the wrong args
 * or result to a step. Everything is read defensively from the free-form payload.
 */
function roundFor(round: unknown, tool: string): RunRoundDetail | undefined {
  if (!round || typeof round !== 'object') return undefined
  const r = round as {
    tool?: unknown
    args?: unknown
    result_snippet?: unknown
    model_text?: unknown
  }
  if (typeof r.tool !== 'string' || !tool || r.tool !== tool) return undefined
  return {
    args: r.args && typeof r.args === 'object' ? (r.args as Record<string, unknown>) : {},
    resultSnippet: typeof r.result_snippet === 'string' ? r.result_snippet : '',
    modelText: typeof r.model_text === 'string' ? r.model_text : '',
  }
}

/**
 * Map a run status string to a Chinese label + badge tone.
 *
 * `undefined` means "the demo run", which is parked on the approval gate; it is
 * labelled 等待审批 rather than guessing at a real status.
 */
export function runStatusMeta(status: string | undefined): { label: string; tone: string } {
  if (!status) return { label: '等待审批', tone: 'amber' }
  const s = status.toLowerCase()
  if (s.includes('await') || s.includes('pend') || s.includes('pause')) return { label: '待审批', tone: 'amber' }
  if (s.includes('run') || s.includes('progress')) return { label: '进行中', tone: 'emerald' }
  if (s.includes('done') || s.includes('complete') || s.includes('success')) return { label: '已完成', tone: 'blue' }
  // INC14 (Q6) — `aborted` is a REAL verdict value (映射自 aborted/cancelled/
  // canceled), so it gets its own honest label. Tone `amber` (not `red`): a
  // run that was cancelled is not the same as one that crashed. Inserted
  // BEFORE the `fail` check so "cancelled" is not swallowed by "fail"... it
  // isn't, but keeping it adjacent documents the ordering contract.
  if (s.includes('abort') || s.includes('cancel')) return { label: '已中止', tone: 'amber' }
  // INC33 / T02 — `interrupted` / `rejected` are real terminal verdicts in the
  // backend vocabulary (`forgeflow.workspace.store.TERMINAL_STATUSES` =
  // {completed, failed, aborted, interrupted, rejected}). Without their own
  // branch they fell through to the `{ label: status }` tail and **leaked the
  // English status verbatim**. Give them honest Chinese labels + `amber` tone
  // (a run that was interrupted / refused is not a crash — same口径 as aborted).
  if (s.includes('interrupt')) return { label: '已中断', tone: 'amber' }
  if (s.includes('reject')) return { label: '已拒绝', tone: 'amber' }
  if (s.includes('fail') || s.includes('error')) return { label: '失败', tone: 'red' }
  // INC37 —— 兜底绝不把后端英文 `status` 原样漏给用户（那正是「字符的形式」）。
  // 认不出的状态一律显示中文「其他状态」；原始值只在 debug 档 `runDebugFacts` 出现。
  return { label: '其他状态', tone: '' }
}

/** ISO timestamp → a short local string; never renders an invalid date. */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/** Run-level debug facts for the header — only shown in debug mode. */
export function runDebugFacts(detail: RunDetail): string {
  const bits: string[] = []
  if (detail.runtime_mode) bits.push(`runtime=${detail.runtime_mode}`)
  if (typeof detail.total_tokens === 'number') bits.push(`tokens=${detail.total_tokens.toLocaleString()}`)
  if (typeof detail.total_cost_usd === 'number') bits.push(`cost=$${detail.total_cost_usd.toFixed(4)}`)
  if (detail.actor_user_id) bits.push(`actor=${detail.actor_user_id}(${detail.actor_role ?? '?'})`)
  return bits.join(' · ')
}

/* ------------------------------------------------------------------------- *
 * INC14 — result layer adapters (纯函数，无 React，无副作用).
 *
 * These turn ONE real `RunDetail` into the four things the result layer
 * renders. Every one of them is deliberately dumb: no string is invented, no
 * label is translated, no value is defaulted into something that was not in the
 * backend payload. "No data" stays "no data" (an empty list), which the layer
 * shows as its honest empty state.
 * ------------------------------------------------------------------------- */

/**
 * The run's deliverables, **verbatim**. No processing whatsoever — an absent
 * `artifacts` degrades to `[]` (a pre-INC14 payload then shows the honest empty
 * state, never a fabricated result).
 */
export function deriveArtifacts(detail: RunDetail): RunArtifact[] {
  return detail.artifacts ?? []
}

/**
 * 关键结论 — the **prose** lines of the deliverable's 「最终答案」 section.
 *
 * INC19 (R-1 / P0-11 / D-7 S-1) —— 取数**不再**是 `steps[].note`（步骤级！）。
 * 那些 note 常常写着「research.search 完成」「已执行」这类工程话术，一旦渲染在
 * 结果 Tab 首屏，就直接把 AC-2 打红（这正是用户这次看见的问题之一）。
 *
 * 新契约：只读**交付部分**（`partitionArtifactBody(...).deliverable`）里
 * 「最终答案 / 核心结论 / 结论」一节的**非空、非表格、非列表的散文行**：
 *
 *   * 与 `deriveFindings`（取**同一节**里的**列表项**）按**行型互斥**，二者绝不重复；
 *   * **无该节 ⇒ 返回 `[]`**（诚实空态；**不拿别的节顶替**，见 D-7 S-1 反事实注入）；
 *   * 每一行逐字来自产物正文，故属 P0-2「忠实优先于降噪」的管辖范围，不是平台拼装文案。
 *
 * 入参是 **markdown 字符串**（交付部分），不是 `RunDetail`——这是本次语义修订的接口钉。
 */
export function deriveConclusions(markdown: string): RunConclusion[] {
  const lines = (markdown ?? '').split('\n')
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (/^#{1,6}\s*(最终答案|核心结论|结论)\s*$/.test(lines[i].trim())) {
      start = i + 1
      break
    }
  }
  // 没有这一节就没有结论——不把别的章节顶上来冒充（诚实空态）。
  if (start < 0) return []

  const out: string[] = []
  for (let i = start; i < lines.length; i++) {
    const line = lines[i].trim()
    if (HEADING_RE.test(line)) break
    if (!line) continue
    // 表格行不是结论正文（工程表 / 指标表都在此处被排除）。
    if (line.startsWith('|')) continue
    // 列表项归 `deriveFindings`（关键发现）⇒ 行型互斥，避免同一行出现两遍。
    if (/^(?:[-*+]|\d+[.)])\s+/.test(line)) continue
    out.push(line)
  }
  return out
}

/**
 * INC20 / T06 —— 从**文本中**提取 http(s) URL 子串（不再是「整串必须是 URL」）。
 *
 * 现状缺陷（修复对象）：旧 `URL_RE = /^https?:\/\/\S+$/i` 要求整串就是 URL，
 * 于是 payload 里嵌在文本中的 URL（如「来源：https://x.com/a」）被漏掉 ⇒ 历史实测
 * 「5 条真 URL 提出 0 条」。
 *
 * 三道守卫（缺一不可，见设计 §5.5）：
 *   ① **scheme 必须存在** —— 只匹配 `https?://` 开头，不匹配无 scheme 的裸域名，
 *      把「普通文本」误伤面压到极低；
 *   ② **去尾随标点**（`. , ) 。 」 ”` … 等）——「https://x.com/a。」只取到 `a`；
 *   ③ **去重**（沿用调用方的 `seen`）。
 * 资格守卫（`deriveSources`：status==='ok' && executed===true && development_stub!==true）
 * **不变**，仍只读真实执行且非 stub 的 payload。
 */
const URL_SUBSTR_RE = /https?:\/\/[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+/g

/** 尾随标点（中英文，含全角），从匹配到的 URL 子串尾部剥离。 */
const URL_TRAILING_PUNCT_RE = /[.,;:!?'")\]}>。，；：！？、】」》”’…]+$/

/** Recursively collect URL-shaped substrings (dedup + order preserved). */
function collectUrls(value: unknown, seen: Set<string>, out: RunSourceRef[]): void {
  if (typeof value === 'string') {
    const matches = value.match(URL_SUBSTR_RE)
    if (!matches) return
    for (const raw of matches) {
      const url = raw.replace(URL_TRAILING_PUNCT_RE, '')
      if (!url || seen.has(url)) continue
      seen.add(url)
      out.push({ kind: 'web', label: url })
    }
    return
  }
  if (Array.isArray(value)) {
    for (const v of value) collectUrls(v, seen, out)
    return
  }
  if (value && typeof value === 'object') {
    for (const v of Object.values(value as Record<string, unknown>)) collectUrls(v, seen, out)
  }
}

/**
 * 来源 — ONLY the URLs a REAL tool call actually returned (Q4).
 *
 * A call qualifies iff it succeeded (`status === 'ok'`) *and* really ran
 * (`executed === true`) *and* was **not** a development stub. This deliberately
 * excludes the dev-stub `research.search`, whose payload carries a fake
 * `https://example.com/mock` URL — treating that as a source would be
 * fabrication. A tool id is NEVER a source (that would be a category error);
 * offline runs therefore yield `[]` and the layer shows the approved
 * 「本次运行未记录可展示的来源」.
 */
export function deriveSources(detail: RunDetail): RunSourceRef[] {
  const invocations = detail.tool_invocations ?? []
  const seen = new Set<string>()
  const out: RunSourceRef[] = []
  for (const inv of invocations) {
    if (inv.status !== 'ok' || inv.executed !== true || inv.development_stub === true) continue
    collectUrls(inv.payload, seen, out)
  }
  return out
}

/**
 * 本次运行结论 — map the run `outcome` to a Chinese label + badge tone (Q6).
 *
 * Only the verdict values that **really exist** get a label
 * (`success` / `failure` / `aborted`). Any unknown value is returned **as-is** —
 * inventing a vocabulary for a status the runtime never emits would be
 * fabrication.
 */
export function outcomeMeta(outcome: string): { label: string; tone: string } {
  switch ((outcome ?? '').toLowerCase()) {
    case 'success':
      return { label: '已完成', tone: 'emerald' }
    // INC18 — the run finished without errors but left an applicable step unrun.
    // Calling that 已完成 is the exact lie this increment removes: the verdict
    // now carries `partial` and the badge must say so. Tone amber (not red):
    // nothing failed, the delivery is simply incomplete.
    case 'partial':
      return { label: '部分完成', tone: 'amber' }
    case 'failure':
      return { label: '失败', tone: 'red' }
    case 'aborted':
      return { label: '已中止', tone: 'amber' }
    default:
      return { label: outcome ?? '', tone: '' }
  }
}

/* ------------------------------------------------------------------------- *
 * INC18 — 第三层「数据依据」 + 「未完成步骤」。
 *
 * Both are projections of data the backend already returns; neither invents
 * anything. The evidence list is 1:1 with `tool_invocations` (L2), and the
 * unrun list mirrors `validation.validator._unrun_steps` exactly — including
 * its two conservative rules: `not_applicable` is NOT a shortfall, and an
 * unknown / absent status is not claimed either way.
 * ------------------------------------------------------------------------- */

/** Chinese label for a step/invocation status; unknown values stay verbatim. */
export function statusLabel(status: string | undefined): string {
  switch ((status ?? '').toLowerCase()) {
    case 'ok':
    case 'success':
    case 'done':
      return '成功'
    case 'error':
    case 'failed':
    case 'failure':
      return '失败'
    case 'blocked':
    case 'skipped':
      return '受阻'
    case 'unavailable':
      return '不可用'
    case 'refused':
      return '被拒绝'
    case 'not_applicable':
      return '未适用'
    case 'running':
    case 'in_progress':
    case 'started':
      return '进行中'
    case 'pending':
      return '待执行'
    default:
      return status ?? '—'
  }
}

/**
 * Statuses that mean "this step should have run, and provably did not".
 *
 * ⚠️ 与后端 `validator._UNRUN_STATUSES` 逐字一致（含边界）：`unavailable` /
 * `refused` 属于既有失败-不可用口径，**不**算未执行，否则前端徽章与后端失败
 * 统计会互相打架。
 */
const UNRUN_STATUSES = new Set([
  'blocked',
  'pending',
  'running',
  'in_progress',
  'started',
  'queued',
  'awaiting_approval',
  'pending_approval',
  'paused',
])
const RAN_STATUSES = new Set(['ok', 'success', 'done', 'error', 'failed', 'failure'])

/** 受阻状态集合（后端 `blocked`，含已退休的 `skipped` 读侧别名——与实时口径一致）。 */
const BLOCKED_STATUSES = new Set(['blocked', 'skipped'])

/**
 * 未完成步骤 — the tools the plan committed to but that never ran.
 *
 * `not_applicable` is excluded on purpose (a step that does not apply to this
 * task is not a shortfall) and so is an unknown / absent status (no evidence
 * either way ⇒ we do not claim it). Mirrors the backend validator verbatim, so
 * the badge, the reason line and the verdict can never disagree.
 */
export function deriveUnrunSteps(detail: RunDetail): string[] {
  const out: string[] = []
  ;(detail.steps ?? []).forEach((step, i) => {
    const s = (step.status ?? '').trim().toLowerCase()
    if (RAN_STATUSES.has(s) || s === 'not_applicable' || s === 'na') return
    if (!UNRUN_STATUSES.has(s)) return
    const label = (step.tool ?? '').trim() || (step.step_id ?? '').trim()
    out.push(label || `步骤 ${i + 1}`)
  })
  return out
}

/**
 * 未完成步骤的**业务名**（INC19 / D-4）——给结果 Tab 的「部分完成」点名用。
 *
 * 过滤谓词 = `deriveUnrunSteps` **再排除受阻步骤**（INC23）：沿用 `RAN_STATUSES` /
 * `UNRUN_STATUSES` 与 `not_applicable`（含 `na`）排除、未知状态不判定的边界，**另外**
 * 剔除 `BLOCKED_STATUSES` 中的 `blocked`——受阻步骤由 `deriveMissingInputs` 专责呈现
 * （原因 + 补齐后重跑），此处不再重复点名。
 *
 * ⚠️ 精确边界（如实声明）：本函数里 `skipped` **不可达** —— `UNRUN_STATUSES` 不含
 * `skipped`，函数在更早的 `if (!UNRUN_STATUSES.has(s)) return` 就已返回，故末尾的
 * `if (BLOCKED_STATUSES.has(s)) return` 实际只会命中 `blocked`。保留 `BLOCKED_STATUSES`
 * （含退役别名 `skipped`）是**防御性冗余**：若日后 `UNRUN_STATUSES` 改变，退役别名仍
 * 不会误入本清单。真正吃 `skipped` 的是 `deriveMissingInputs`（它直接用 `BLOCKED_STATUSES`
 * 过滤，`skipped` 在那里**可达**）。
 *
 * 输出物与 `deriveUnrunSteps` 不同：`deriveUnrunSteps` 直出 `step.tool`（如
 * `report.render`）——那是工程词，正是本次要清出首屏的东西；本函数改为**业务名**：
 *
 *   1. 在 `detail.plan?.steps` 里按 `index`（优先）或 `tool` 匹配一条；
 *   2. 取其**非空 `note`**（trim）作为业务名（如「生成报告」）；
 *   3. 匹配不到 / note 为空 / `containsPlatformToolId(note)` ⇒ 用**「第 N 步」**；
 *   4. **绝不**回落到 `step.tool` / `step.step_id`（工程词）。
 *
 * ⚠️ 护栏的**残留极限（如实声明）**：`containsPlatformToolId` 只抓「平台工具 id 的
 * 句型」（`a.b`），抓不到平台生成的**中文工程词**（如某 note 写成「已执行」）。后者
 * 由结果 Tab 的 **AC-2a 结构断言**与**探针词表**兜底，不在本谓词职责范围内——职责
 * 边界写清，避免被当作「已全面防护」。
 *
 * 注意：**不得**用本函数替换 `deriveUnrunSteps` —— `deliveryState` 依赖后者做计数
 * （后者**仍含**受阻步骤，是 verdict 同源口径，勿改）。
 */
/**
 * 一个运行步骤在 `detail.plan?.steps` 里的匹配项（按 `index` 优先、`tool` 回落）。
 *
 * 抽为共享实现，供 `deriveUnrunStepLabels`（未完成步骤业务名）与
 * `deriveMissingInputs`（受阻步骤清单）**同一口径**取数，避免两处漂移。
 */
function matchPlanStep(
  step: RunDetail['steps'][number],
  planSteps: { index?: number; tool?: string; note?: string; blocked_reason?: string }[],
  i: number,
): { index?: number; tool?: string; note?: string; blocked_reason?: string } | undefined {
  const stepIndex = typeof step.index === 'number' ? step.index : i
  let matched = planSteps.find((p) => typeof p.index === 'number' && p.index === stepIndex)
  if (!matched) {
    const tool = (step.tool ?? '').trim()
    if (tool) matched = planSteps.find((p) => (p.tool ?? '').trim() === tool)
  }
  return matched
}

/**
 * 一个步骤的**业务名**（INC19 / D-4 口径，抽为共享实现）：
 *   1. 在 `detail.plan?.steps` 里按 `index`（优先）或 `tool` 匹配一条；
 *   2. 取其**非空 `note`**（trim）作为业务名（如「生成报告」）；
 *   3. 匹配不到 / note 为空 / `containsPlatformToolId(note)` ⇒ 用**「第 N 步」**；
 *   4. **绝不**回落到 `step.tool` / `step.step_id`（工程词）。
 */
function businessStepLabel(
  step: RunDetail['steps'][number],
  planSteps: { index?: number; tool?: string; note?: string; blocked_reason?: string }[],
  i: number,
): string {
  const note = (matchPlanStep(step, planSteps, i)?.note ?? '').trim()
  return note && !containsPlatformToolId(note) ? note : `第 ${i + 1} 步`
}

export function deriveUnrunStepLabels(detail: RunDetail): string[] {
  const planSteps = detail.plan?.steps ?? []
  const out: string[] = []
  ;(detail.steps ?? []).forEach((step, i) => {
    const s = (step.status ?? '').trim().toLowerCase()
    if (RAN_STATUSES.has(s) || s === 'not_applicable' || s === 'na') return
    if (!UNRUN_STATUSES.has(s)) return
    // INC23 — 受阻步骤由 `deriveMissingInputs` 专责呈现；此处剔除，否则同一受阻步骤
    // 会在「部分完成」(result-partial) 与「缺失输入」(result-missing-inputs) 印两遍。
    if (BLOCKED_STATUSES.has(s)) return
    out.push(businessStepLabel(step, planSteps, i))
  })
  return out
}

/**
 * INC22 W3.2 —— 结果页「缺失输入」清单里的一条：**受阻**（blocked）步骤。
 *
 * `reason` / `tool` 都是后端原文；`label` 是业务名（见 `businessStepLabel`）。
 */
export type RunMissingInput = {
  /** 受阻步骤的业务名（与「部分完成」同口径；绝不直出平台工具 id）。 */
  label: string
  /**
   * 后端 `blocked_reason` **原文**（逐字，不美化 / 不翻译 / 不截断）；
   * `''` 表示后端（`steps[]` 与 `plan.steps[]`）都没有为这一步记录原因。
   */
  reason: string
  /** 后端 `step.tool` 原文，仅作 `title` 悬浮诊断（不进可见行）。 */
  tool: string
}

/**
 * INC22 W3.2 —— 结果页的「缺失输入」清单：逐条列出**受阻**（blocked）步骤。
 *
 * 只取后端真实字段，**绝不臆造**：
 *   * `label`  —— 受阻步骤的业务名（`businessStepLabel`，与「部分完成」同口径）；
 *   * `reason` —— 受阻原因**原文**（**逐字**，不美化 / 不翻译 / 不截断）。取数优先级
 *     （四层**按序回落**，任一层有值即用；INC23 起 L1/L2 两侧同值，故 1/2 通常即命中）：
 *       1. `plan.steps[]` 里按 `index` / `tool` 匹配到的那条的 `blocked_reason`
 *          （`planning.py::PlanStep.to_dict()` 写入）；
 *       2. `steps[].blocked_reason`（`planning.py::PlanStep.to_payload` 写入，与 1 同值）；
 *       3. 该步骤对应的 `tool_invocations[]` 记录的 `payload.reason`（**完整原文**，
 *          `tool_executor.py::ToolExecutor.execute` 写入 —— L2 真源）；
 *       4. 同上记录的 `error`（`tool_executor.py::ToolExecutor.execute` 写入，与 `reason` 同值）。
 *     四者皆空 ⇒ `''`（界面只出业务名、不出伪原因）。
 *   * `tool`   —— `step.tool` 原文，仅供 `title`，**不进可见行**（AC-2b：平台工具 id
 *     不得占据结果 Tab 首屏）。
 *
 * 过滤口径：仅 `status ∈ {blocked, skipped}`（受阻——非失败、非未适用）。无受阻步骤 ⇒ `[]`。
 *
 * ⚠️ 已知边界（如实声明，**非本轮引入**、INC23 前后一致）：本函数只遍历 `detail.steps`。
 * 若某受阻步骤**只出现在 `plan.steps` 而 `steps[]` 里缺失**，则它既不在「部分完成」
 * （`deriveUnrunStepLabels`）也不在本「缺失输入」清单里 —— 这是上游 `steps[]` 与
 * `plan.steps[]` 数据不一致所致的盲区。本函数**据实不臆造**（不凭空补一条受阻项），
 * 故此处只声明边界、不修代码。
 */
export function deriveMissingInputs(detail: RunDetail): RunMissingInput[] {
  const planSteps = detail.plan?.steps ?? []
  const invocations = detail.tool_invocations ?? []
  const out: RunMissingInput[] = []
  ;(detail.steps ?? []).forEach((step, i) => {
    const s = (step.status ?? '').trim().toLowerCase()
    if (!BLOCKED_STATUSES.has(s)) return
    const tool = (step.tool ?? '').trim()
    const stepIndex = typeof step.index === 'number' ? step.index : i
    const matched = matchPlanStep(step, planSteps, i)
    const fromPlan = (matched?.blocked_reason ?? '').trim()
    const fromStep = typeof step.blocked_reason === 'string' ? step.blocked_reason.trim() : ''
    // 该步骤对应的调用记录（`step_id` 形如 `"{run_id}:{attempt}:{index}"`，按尾段 `:index` 匹配，
    // 与 `detailToStages` 同法；工具相同进一步收窄，避免跨步骤错配）。
    const inv = invocations.find(
      (v) =>
        typeof v.step_id === 'string' &&
        v.step_id.endsWith(`:${stepIndex}`) &&
        (!tool || v.tool === tool),
    )
    const payload = inv?.payload
    const fromInv =
      payload && typeof payload === 'object' && typeof (payload as Record<string, unknown>).reason === 'string'
        ? ((payload as Record<string, unknown>).reason as string).trim()
        : ''
    const fromErr = typeof inv?.error === 'string' ? inv.error.trim() : ''
    out.push({
      label: businessStepLabel(step, planSteps, i),
      reason: fromPlan || fromStep || fromInv || fromErr,
      tool,
    })
  })
  return out
}

/** A bounded, verbatim rendering of one invocation's recorded return. */
const SNIPPET_CHARS = 400

/**
 * 数据依据 — one entry per L2 invocation, in the order the runtime recorded them.
 *
 * Nothing is inferred: the snippet is the invocation's own recorded payload (the
 * same PI-sanitised text the model was fed back), bounded for display. A call
 * that never executed has nothing to cite, so its snippet stays empty and the
 * row says so rather than showing a placeholder number.
 */
export function deriveEvidence(detail: RunDetail): RunEvidence[] {
  const invocations = detail.tool_invocations ?? []
  return invocations.map((inv, i) => {
    const tool = typeof inv.tool === 'string' && inv.tool ? inv.tool : '—'
    const status = typeof inv.status === 'string' ? inv.status : ''
    const raw = (inv as Record<string, unknown>).payload
    let snippet = ''
    if (typeof raw === 'string') snippet = raw
    else if (raw && typeof raw === 'object') snippet = JSON.stringify(raw)
    const ref = (inv as Record<string, unknown>).result_ref
    return {
      id: typeof inv.step_id === 'string' && inv.step_id ? inv.step_id : `inv-${i}`,
      tool,
      // INC20 / P1-1 —— 证据条目的**业务主标识**（角色展示名 / 真实工具 id），
      // 与工作链一致，可回溯。`tool` 仍保留在数据里（作为 title 提示）。
      bizLabel: roleForTool(tool),
      status,
      statusLabel: statusLabel(status),
      executed: inv.executed === true,
      ms: measuredMs(inv.latency_ms),
      snippet: snippet.slice(0, SNIPPET_CHARS),
      resultRef: typeof ref === 'string' && ref ? ref : null,
      // A stub's return is real *as a record* but is not real-world data; the
      // UI labels it so a mock URL can never read as a genuine source.
      stub: (inv as Record<string, unknown>).development_stub === true,
    }
  })
}

/**
 * INC20 / T06 (P1-5) —— 证据 Tab 的**计数汇总头**（防假计数）。
 *
 * 口径定义：`N = sources.length`（`deriveSources` 去重后输出）、
 * `M = evidence.length`（`deriveEvidence` 输出，**严格 = tool_invocations.length**，1:1）。
 * 两数**就是已渲染列表的 `.length`**，与列表内容天然一致 ⇒ 不存在「计数与列表不符」
 * 的假计数。
 *
 * 渲染条件由调用方承担：**仅当 `N + M > 0` 时渲染**，否则不渲染（**绝不**显示
 * 「0 个来源」）。术语用既有 UI 术语「来源」「数据依据」，不引入第三套词汇。
 */
export type EvidenceSummary = { sources: number; evidence: number }

export function deriveEvidenceSummary(
  sources: RunSourceRef[],
  evidence: RunEvidence[],
): EvidenceSummary {
  return { sources: sources.length, evidence: evidence.length }
}

/**
 * INC20 / P1-3 —— `Experience`（`GET /experiences?run_id=` 的响应项）→ 展示用
 * **只读投影** `RunExperienceItem`。只取成本/记忆 Tab 真正要渲染的字段；
 * `memory_ids` 为**id 级**记忆列表（内容级需后端补 run 过滤，本期不做，见 P2-3）。
 */
export function projectExperience(e: Experience): RunExperienceItem {
  return {
    id: e.id,
    summary: e.summary ?? '',
    outcome: e.outcome ?? '',
    tags: e.tags ?? [],
    memoryIds: e.memory_ids ?? [],
    createdAt: e.created_at ?? '',
  }
}

/* ------------------------------------------------------------------------- *
 * INC18-B — 第二层：把 L4 产物正文**结构化呈现**（不改写、不编造）。
 *
 * 三者都只读 `artifacts[*].content`：标题 → 章节，列表 → 关键发现，表格 → 指标。
 * 正文里没有的东西不会出现在界面上（因此没有真实业务数据源时，指标卡**不会**出现）。
 * ------------------------------------------------------------------------- */

const HEADING_RE = /^(#{1,6})\s+(.+?)\s*$/

/**
 * INC19 — section titles that are the platform's own engineering ledger.
 *
 * These five are the ones the user saw twice (once inside the artifact body,
 * once as step cards). They are genuinely useful — to an operator — so they are
 * **moved, not deleted**: they now live in the 执行轨迹 tab (both as the
 * `ExecutionSection` step cards and as the verbatim `execution-ledger` block).
 *
 * ⚠️ 这份白名单是 D-1 的**唯一**判定依据（纯标题匹配，**不加**「含英文/含表格」这类
 * 启发式）。fail-closed 的方向是「可见」：未命中白名单的 `##` 节一律归**交付部分**，
 * 宁可它可见地留在结果 Tab 被 AC-2a 当场抓到，也不要被模糊规则静默吞进轨迹。
 */
const ENGINEERING_SECTION_TITLES = ['执行记录', '任务计划', '未适用', '受阻', '失败']

/** Whether a Markdown heading is the platform's engineering ledger. */
export function isEngineeringSection(title: string): boolean {
  const t = (title ?? '').trim()
  return ENGINEERING_SECTION_TITLES.some((w) => t.includes(w))
}

/* ------------------------------------------------------------------------- *
 * INC19 — 分区：结果是主角，过程是证据。
 *
 * 产物正文由后端 `report.render`（`tool_handlers._render_full_report`）一次生成，
 * 交付内容与工程账本**混在同一篇**里。本组函数**只做归属分桶**——按真实 Markdown
 * 二级标题把正文切成连续行区间，命中白名单的整节入 `engineering`，其余（含前言）
 * 入 `deliverable`；**不改写、不摘要、不翻译、不重排任何一字**（D-1 / D-3）。
 * ------------------------------------------------------------------------- */

/** 一节（连续行区间）的归属。 */
export type BodySegment = {
  /** 起始行下标（含），0-based。 */
  start: number
  /** 结束行下标（不含）。 */
  end: number
  /** 归属容器。 */
  kind: 'deliverable' | 'engineering'
  /** 节标题文本（前言为空串），仅用于目录/诊断，不参与渲染正文。 */
  title: string
}

/** 一次分区的结果：两个逐字正文桶 + 可证伪的行区间见证。 */
export type BodyPartition = {
  /** 交付部分：前言 + 未命中白名单的 `##` 节，按原顺序逐字拼接（含标题行）。 */
  deliverable: string
  /** 工程账本部分：命中白名单的 `##` 节 + 其后的尾随无标题内容，按原顺序逐字拼接。 */
  engineering: string
  /** 原正文的连续行区间划分（覆盖 `[0, lines.length)`），用于无损性校验。 */
  segments: BodySegment[]
}

/** 恰好两个 `#` + 一个空白字符的二级标题行（**只认这一种**，不认 `#` / `###`）。 */
const H2_RE = /^##\s/

/**
 * 只读纯函数：把产物正文按**真实二级标题**分成「交付 / 工程」两桶。
 *
 * 算法（严格按设计 §2.2）：
 *   1. `split('\n')` 切行，**不 trim 行内内容**（丢字符即违反逐字纪律）；
 *   2. 边界行 = 匹配 `/^##\s/` 的行；无边界 ⇒ 全篇归 `deliverable`；
 *   3. 第一个边界之前 ⇒ 前言区间（`kind: 'deliverable'`, `title: ''`）；
 *   4. 每个边界到下一个边界（最后一个到 `lines.length`）为一个区间，
 *      `kind = isEngineeringSection(title)`；
 *   5. 各桶内按原顺序、逐字 `join('\n')` 拼接。
 *
 * 尾随无标题内容归其**前一节**：当前产物末节恒为 `## 五、失败`（工程）⇒ 页脚
 * （`共 N 步计划：…` / `说明：…`）自然进工程桶，满足 D-1。
 *
 * 无损性（可证伪）：`segments` 覆盖 `[0, lines.length)`、连续不重叠有序；把所有
 * 区间按 `start` 升序 `join('\n')` 拼回 **=== 原始 markdown（byte-for-byte）**。
 *
 * 已知边界（QA 证伪 R-9.2，**只记录、不修**；参见 decisions 文档 INV-19-4 与 D-1）：
 *   模型若在**最终答案正文里自带**一行白名单标题（如 `## 一、执行记录`），该行会被本
 *   函数当作真实节边界 ⇒ **它之后的、仍属模型答案的那一段文本被归入 `engineering`
 *   桶（放错桶）**。这是本分区的**已知且被接受**的边界。
 *
 *   后果是**有界**的：文本**不会丢失** —— 放错桶的那段在执行轨迹 Tab 的账本
 *   （`execution-ledger`）里**仍可逐字取得**（页面级可达性即 decisions 文档的
 *   **INV-19-4**）；这**不是**静默隐藏。
 *
 *   **不声称有兜底**：**AC-2a 抓不到这一类切错** —— 已被实测证伪，两条原因叠加：
 *   ① 真实平台标题带「一、」前缀，字面 `## 执行记录` 本就不在源文本里；
 *   ② `ResultMarkdown` 把 `## ` 渲染成 `<h2>` ⇒ **DOM 里连 `## ` 字面都不存在**
 *   （实测 `has_hash_hash === false`）。因此**不存在**任何「会被某断言抓到」的保证。
 *
 *   为什么不修：要区分「平台写的标题」与「模型写的标题」，**只有文本时不可判定**；
 *   任何启发式（含英文、含表格、按位置）都会破坏 **D-1「只认白名单、未命中一律归
 *   交付」的 fail-closed 方向** —— 宁可多显示业务内容，也**不静默吞掉**。
 *
 *   口径已迁移（**AC-2a′**，在结果 Tab 子树 `#res-panel-result` 内判，分两半）：
 *     · 结构：子树内**不得存在**「表头单元格**同时**含 `工具` + `状态` + `已执行`」的 `<table>`；
 *     · 文本：`共 N 步计划` 与 `本报告由产物步 report.render 生成` 不得出现在子树 `innerText`。
 *
 *   ⚠️ 执行记录表头行的**字面**不得再当标记 —— `ResultMarkdown` 开 GFM，该行被渲染成
 *   `<table>`，`textContent` 里没有 `|` 字面 ⇒ **判别力 0**（反事实注入实测：屏幕上有平台表
 *   而断言全绿）。
 *   ⚠️ 「在账本 `<pre>` 里能找到该标记 ⇒ 证明匹配有效」**无效** —— `<pre>` 装原文 markdown、
 *   结果 Tab 装渲染后文本，**跨上下文不可外推**；非空转证明一律改用「反事实注入必须变红」。
 *   ⚠️ 页面头部（`RunHeader`）在 AC-2/AC-2b **测量范围之外** —— 头部 `<h1>` 是**用户原话
 *   intent**、concise meta 用 `real.status` 的**英文原值**（`failed`/`blocked`），拿词表判头部
 *   必然**假红**。
 */
export function partitionArtifactBody(markdown: string): BodyPartition {
  const src = markdown ?? ''
  const lines = src.split('\n')

  // 1) 找出所有二级标题边界行。
  const bounds: number[] = []
  for (let i = 0; i < lines.length; i++) {
    if (H2_RE.test(lines[i])) bounds.push(i)
  }

  // 2) 切成连续、不重叠、覆盖全行的区间序列。
  const segments: BodySegment[] = []
  if (bounds.length === 0) {
    // 退化：全篇无 `##` ⇒ 全部归交付（`engineering` 为空串）。
    segments.push({ start: 0, end: lines.length, kind: 'deliverable', title: '' })
  } else {
    if (bounds[0] > 0) {
      // 前言：`# 运行报告` / `**意图**：…`。
      segments.push({ start: 0, end: bounds[0], kind: 'deliverable', title: '' })
    }
    for (let k = 0; k < bounds.length; k++) {
      const s = bounds[k]
      const e = k + 1 < bounds.length ? bounds[k + 1] : lines.length
      // 标题文本仅去标记，用于分类；渲染仍用原行（不改写）。
      const title = lines[s].replace(/^##\s+/, '').trim()
      const kind: BodySegment['kind'] = isEngineeringSection(title) ? 'engineering' : 'deliverable'
      segments.push({ start: s, end: e, kind, title })
    }
  }

  // 3) 分桶：各桶内保持原文相对顺序，整体逐字拼接。
  const joinKind = (kind: BodySegment['kind']): string =>
    segments
      .filter((seg) => seg.kind === kind)
      .map((seg) => lines.slice(seg.start, seg.end).join('\n'))
      .join('\n')

  return {
    deliverable: joinKind('deliverable'),
    engineering: joinKind('engineering'),
    segments,
  }
}

/**
 * 平台工具 id 的**句型**（如 `research.search` / `report.render`）——D-7.1 的
 * 结构判定。**不复制 AC-2b 词表**（那会造成第三处会漂移的词表副本）：真正的风险是
 * 「平台工具 id 漏到首屏」，而工具 id 有稳定句型（`a.b` 小写点分），句型判定即可
 * 精确定位，且不会误伤业务措辞。
 *
 * 统一口径：`result-partial` 的业务名护栏与 `result-metrics` 的 `rest` 抑制
 * **复用同一个谓词**。
 */
const PLATFORM_TOOL_ID_RE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/

/** 该串本身就是一个平台工具 id（前后空白忽略）。 */
export function isPlatformToolId(s: string): boolean {
  return PLATFORM_TOOL_ID_RE.test((s ?? '').trim())
}

/** 工具 id 的形状判定的 token 分隔符（中英文标点 + 空白 + 竖线 + 间隔号）。 */
const TOOL_TOKEN_SPLIT_RE = /[\s、,，;；|·]+/

/** 任一 token 是工具 id ⇒ 该串含工程标识（如 `research.search 返回 3 条`）。 */
export function containsPlatformToolId(s: string): boolean {
  return (s ?? '')
    .split(TOOL_TOKEN_SPLIT_RE)
    .some((tok) => isPlatformToolId(tok))
}

/**
 * 成果章节 — split the artifact body by its REAL Markdown headings.
 *
 * Only headings that actually exist in the deliverable are produced, so a
 * one-section artifact yields one card instead of an invented outline.
 */
export function deriveSections(markdown: string): RunSection[] {
  const lines = (markdown ?? '').split('\n')
  const out: RunSection[] = []
  let cur: RunSection | null = null
  for (const line of lines) {
    const m = HEADING_RE.exec(line)
    if (m) {
      if (cur) out.push(cur)
      cur = {
        id: `sec-${out.length}`,
        level: m[1].length,
        title: m[2],
        body: '',
        engineering: isEngineeringSection(m[2]),
      }
      continue
    }
    // 表格行不进摘要预览：一个纯表格的章节（如「执行记录」）会被下面的空正文
    // 过滤掉，因此「成果结构」只呈现有可读内容的章节，而不是把工程账本列出来。
    if (cur && !line.startsWith('|')) cur.body += cur.body ? `\n${line}` : line
  }
  if (cur) out.push(cur)
  // A heading whose body is empty carries no information (a decorative
  // divider) — dropped rather than rendered as an empty card.
  return out.filter((s) => s.body.trim().length > 0)
}

/** Split one table row into trimmed cells. */
function rowCells(line: string): string[] {
  return line
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim())
}

/**
 * 指标 — the artifact's first REAL *business* Markdown table, one card per row.
 *
 * Two honest guards (both matter):
 *
 *  1. no table at all ⇒ `[]`. We show no metric card rather than a placeholder
 *     number; every value here is a verbatim cell of the real report.
 *  2. **an engineering table is not a business metric.** Today's
 *     `report.render` output leads with the execution ledger
 *     (`| # | 工具 | 状态 | 已执行 | provider | 延迟(ms) | 摘要 |`); rendering
 *     those rows as "指标卡" would put engineering fields on the first screen —
 *     exactly what this redesign removes. Such a table is refused here.
 *
 * So on a platform run without a real business data source this returns `[]`
 * by design: the metric cards appear the moment a deliverable genuinely
 * carries business numbers, and never before.
 */
const ENGINEERING_HEADER = [
  '工具', '状态', '已执行', 'provider', '延迟', '摘要', '执行记录',
  'tool', 'status', 'executed', 'latency', 'step',
]

export function deriveMetrics(markdown: string): RunMetric[] {
  const lines = (markdown ?? '').split('\n')
  const blocks: string[][] = []
  let cur: string[] = []
  for (const raw of lines) {
    const line = raw.trim()
    if (line.startsWith('|')) {
      cur.push(line)
      continue
    }
    if (cur.length >= 3) blocks.push(cur)
    cur = []
  }
  if (cur.length >= 3) blocks.push(cur)

  for (const block of blocks) {
    // A Markdown table needs a header, a `|---|` separator and ≥1 data row.
    if (!/^\|?[\s:|-]+\|?$/.test(block[1])) continue
    const header = rowCells(block[0])
    if (header.length < 2) continue
    const joined = header.join(' ').toLowerCase()
    if (ENGINEERING_HEADER.some((w) => joined.includes(w.toLowerCase()))) continue
    return block.slice(2).map((row) => {
      const cells = rowCells(row)
      return { label: cells[0] ?? '', value: cells[1] ?? '', rest: cells.slice(2) }
    })
  }
  return []
}

/**
 * 关键发现 — the numbered / bulleted items that really appear in the artifact.
 *
 * INC19 契约：入参是**交付部分**（`partitionArtifactBody(...).deliverable`），
 * 与 `deriveConclusions`（取**同一节的散文行**）**按行型互斥**，二者绝不重复。
 * 只从「最终答案 / 结论」这一节里取列表项；任务计划与执行记录同样写成列表，但那是
 * 工程步骤（`research.search` …），且它们已被分区移出交付部分，不可能再被当「关键发现」。
 *
 * Bullets inside a table-shaped line are ignored; nested list markers are kept
 * verbatim. Empty when the deliverable has no list (no invented findings).
 */
export function deriveFindings(markdown: string): string[] {
  const lines = (markdown ?? '').split('\n')
  // 只从「最终答案 / 结论」这一节里取。任务计划与执行记录同样写成列表，但那是
  // 工程步骤（research.search …），把它们当「关键发现」正是本次要去掉的做法。
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (/^#{1,6}\s*(最终答案|核心结论|结论)\s*$/.test(lines[i].trim())) {
      start = i + 1
      break
    }
  }
  // 没有这一节就没有发现——不把别的章节的列表顶上来冒充。
  if (start < 0) return []

  const out: string[] = []
  for (let i = start; i < lines.length; i++) {
    const line = lines[i].trim()
    if (HEADING_RE.test(line)) break
    if (line.startsWith('|')) continue
    const m = /^(?:[-*+]|\d+[.)])\s+(.+)$/.exec(line)
    if (m) {
      const text = m[1].trim()
      if (text) out.push(text)
    }
  }
  return out
}

/* ------------------------------------------------------------------------- *
 * INC18-B — 交付状态六态（**仅前端派生**，不新增后端状态值）。
 * ------------------------------------------------------------------------- */

// INC33 / T02 — mirror the backend's terminal vocabulary
// (`forgeflow.workspace.store.TERMINAL_STATUSES`): `interrupted` / `rejected`
// are terminal too, so a hydrated-after-restart run is not mislabelled
// 「进行中」 just because it has no `completed_at`.
const TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
  'aborted',
  'interrupted',
  'rejected',
  'success',
])

/**
 * 交付状态 — what the user is actually looking at, in one word.
 *
 * Priority is deliberate: a run that is still going is `waiting` even if it
 * already recorded something; a real failure outranks an incomplete delivery;
 * a step parked on a human is its own state (it is neither done nor broken).
 * `partial` is the INC18 fix for "one step ran ⇒ 已完成".
 */
export function deliveryState(detail: RunDetail): {
  state: DeliveryState
  label: string
  tone: string
} {
  const status = (detail.status ?? '').toLowerCase()
  const errors = detail.errors ?? []
  const steps = detail.steps ?? []
  const artifacts = detail.artifacts ?? []
  const outcome = (detail.outcome ?? '').toLowerCase()

  const statuses = steps.map((s) => (s.status ?? '').toLowerCase())
  const needsApproval = statuses.some((s) =>
    ['awaiting_approval', 'pending_approval', 'paused'].includes(s),
  )
  const unrun = deriveUnrunSteps(detail)

  if (!TERMINAL_STATUSES.has(status) && !detail.completed_at) {
    return { state: 'waiting', label: '进行中', tone: 'blue' }
  }
  // INC32 修复 —— abort 是一个**真实终态**（`POST /runs/{id}/abort`，T02 起可达）。
  // 若不在此拦截，`aborted` 会穿透到末尾被当成「已完成」（或更早被 unrun 判成「部分
  // 完成」），与 `RunHeader` 的「已中止」徽标自相矛盾。口径必须与 `runStatusMeta` /
  // `outcomeMeta` 完全一致（「已中止」/ amber）。放在 `failed` **之前**：`cancelled`
  // 不应被 `fail` 吞掉（沿用 `runStatusMeta` 的相邻顺序契约）。
  if (status.includes('abort') || status.includes('cancel') || outcome === 'aborted') {
    return { state: 'aborted', label: '已中止', tone: 'amber' }
  }
  // INC33 / T02 — `interrupted` / `rejected` are the other two terminal verdicts
  // the backend records (`TERMINAL_STATUSES`). Before they were terminal they
  // fell through to 「已完成」/「部分完成」 — the same self-contradiction the
  // `aborted` fix (0a22ebe) removed. Same口径 as `runStatusMeta`.
  if (status.includes('interrupt') || outcome === 'interrupted') {
    return { state: 'interrupted', label: '已中断', tone: 'amber' }
  }
  if (status.includes('reject') || outcome === 'rejected') {
    return { state: 'rejected', label: '已拒绝', tone: 'amber' }
  }
  if (errors.length > 0 || outcome === 'failure') {
    return { state: 'failed', label: '失败', tone: 'red' }
  }
  if (needsApproval) return { state: 'need_approval', label: '待审批', tone: 'amber' }
  if (artifacts.length === 0 && steps.length > 0 && unrun.length === steps.length) {
    return { state: 'blocked', label: '受阻', tone: 'amber' }
  }
  if (outcome === 'partial' || unrun.length > 0) {
    return { state: 'partial', label: '部分完成', tone: 'amber' }
  }
  return { state: 'completed', label: '已完成', tone: 'emerald' }
}

/* ------------------------------------------------------------------------- *
 * INC21 / G1 — 结果 Tab「为什么没有交付内容」的诚实说明来源。
 *
 * 事实源（**后端已给出，无需新增接口**）：`GET /runs/{id}` 的 `llm` 字典里带有
 * `degraded` 字段。生产者 `forgeflow/runtime/orchestrator.py` 在三条路径写入它：
 *   * `orchestrator.py::_llm_executor`  `runtime_meta["degraded"] = "no_model"`
 *     —— 本次运行没有可用的模型，平台以确定性编排执行；
 *   * `orchestrator.py::_llm_executor`  `runtime_meta["degraded"] = "provider_degraded_to_mock"`
 *     —— 配置的 provider 调用失败，已降级到内置模拟（mock）模型；
 *   * `orchestrator.py::_llm_executor`  `runtime_meta["degraded"] = f"exception: {exc}"`
 *     —— 模型调用抛异常，`{exc}` 为原始异常文本。
 * 该 dict 经 `orchestrator.py::_llm_executor` 的 `runtime_meta: dict[str, Any] = {` 建立、
 * `task.context["llm_runtime"] = runtime_meta` 保存，并在 `orchestrator.py::run_task` 的
 * `llm_meta = task.context.get("llm_runtime")` 取回、`llm=dict(llm_meta)` 原样挂到运行记录上，
 * 因此前端 `RunLLM`
 * （`api/client.ts::RunLLM` 带 `[k: string]: unknown` 索引签名）可直接读到
 * `llm.degraded`。
 * ------------------------------------------------------------------------- */

/**
 * 结果 Tab「降级说明」的结果（G1）。`present === false` 时 label/detail/diagnostic 均空。
 *
 * 字段语义（**两类信息严格分开，勿混**）：
 *   * `detail`     = **业务面可见的补充说明** —— 只能是平台自撰的业务句，或 `null`；
 *                    **绝不**承载后端工程原文。
 *   * `diagnostic` = **后端原始诊断串，逐字**（含 `exception: …` 前缀，不美化 / 不截断）
 *                    —— **不铺在可见行**，只由界面放到 `title` 上做悬浮诊断。
 *                    沿用本仓库既有约定：`RunStageCard.tsx::formatMs` 即把
 *                    「该步骤未测量耗时」放在 `title` 承载诊断，可见行只出业务态。
 */
export type DegradeNotice = {
  /** 是否存在**非空字符串**的降级标识（缺失 / 非字符串 / 空串 ⇒ false）。 */
  present: boolean
  /**
   * INC39 —— 降级**种类**（只增不改既有语义）：
   *   * `'env'`      —— **执行环境未就绪**：模型根本未启用这一族。它**不是**模型的
   *                    失败/降级，而是「本次运行没连模型」的环境状态，故由 `ResultEnvStatus`
   *                     在段① 以**执行环境状态**呈现，**不再**当作段④ 的「当前阻塞」，
   *                     也**不再**伪装成段② 的 Agent 结论。
   *   * `'degraded'` —— **真实的模型调用失败 / 降级**（provider 降级到 mock / 调用抛异常 /
   *                     未登记的非空降级值），以及代码执行面降级 —— 保留在段④「当前阻塞」。
   *   * `null`       —— `present === false`（无降级）时的诚实空值。
   *
   * ⚠️ 与 `present` 正交：`present === true` 时 `kind ∈ {'env','degraded'}`；
   * `present === false` 时 `kind === null`。
   */
  kind: 'env' | 'degraded' | null
  /** 面向业务的说明主文案（**绝不**直出工程枚举原值）。 */
  label: string
  /** 业务面**可见**的补充说明（平台自撰业务句），或 `null`。 */
  detail: string | null
  /** 后端**原始诊断串**（逐字），仅供 `title` 悬浮诊断；无诊断时为 `null`。 */
  diagnostic: string | null
}

/**
 * INC21 / G1 —— 把后端 `llm.degraded` 映射为**面向业务**的降级说明。
 *
 * 取值为**非空字符串**才判 `present = true`；缺失 / 非字符串 ⇒
 * `{ present: false, label: '', detail: null, diagnostic: null }`（调用方据此**不渲染**）。
 *
 * 映射（`degraded` 的三个真实取值见上方 `orchestrator.py::_llm_executor` 写 `runtime_meta["degraded"]` 的三处）：
 *   * `no_model`                  ⇒ label「本次运行未启用模型驱动」，
 *                                   detail「平台以确定性编排执行，只记录过程、不生成报告正文。」，
 *                                   diagnostic = `null`。
 *   * `provider_degraded_to_mock` ⇒ label「模型调用未成功，已降级执行」，
 *                                   detail「本次由内置模拟模型接管，未生成真实报告正文。」，
 *                                   diagnostic = `null`。
 *   * 以 `exception` 开头的值     ⇒ label「模型调用异常，已降级执行」，
 *                                   **detail = `null`**，
 *                                   diagnostic = 后端**原始整串**（逐字，含 `exception: ` 前缀）。
 *   * 其它**非空字符串**（未知值） ⇒ label「本次运行已降级执行」，
 *                                   detail = `null`，diagnostic = 原始值**逐字**。
 *
 * 口径：**业务面只出业务表述** —— 后端原始诊断串（工程原文，`exception: …` 可能含
 * 内部 URL / 堆栈片段）**不铺在可见行**，只作 `title` 悬浮诊断。
 *
 * ⚠️ **未知值不臆造**：不得为未登记的 `degraded` 值编造原因，也**不得**把原始工程
 * 枚举直接当作主文案（label 一律走业务表述；未知枚举只在 hover 露出）。
 *
 * INC22 W3.4 —— 新增**可选**第二入参 `runtimeMode`：当后端**没有**写 `degraded`
 * 字符串、但本次运行**根本不属于模型驱动档**（离线编排档）时，同样必须如实告知
 * 「未启用模型驱动」——否则用户看到「已完成 + 没有产出」却不知是因为压根没连模型。
 * 判据复用 `roles.isModelDriven`（**不另造第二套**；`graph` 属模型驱动档 ⇒ 不会被
 * 误报）。向后兼容：省略该入参 ⇒ 保持 INC21 既有语义（`present=false`），既有单参
 * 调用与其哨兵测试不受影响。
 *
 * @param llm `GET /runs/{id}` 的 `llm` 字典（`RunLLM`，可含 `degraded`）。
 * @param runtimeMode `GET /runs/{id}` 的 `runtime_mode`（可选）；仅在**明确传入**时
 *   参与判定（省略 ⇒ 退回 INC21 的单参语义）。
 * @param codeplane INC25 —— `GET /runs/{id}` 的 `codeplane` 字典（可选）。仅当其中
 *   `degraded` 为非空字符串时，才算「代码执行面降级」并**优先**返回其降级说明；
 *   省略 / 空 ⇒ 与既有单参语义**逐字一致**（既有调用与哨兵测试不受影响）。
 *
 * INC39 —— 新增 `kind: 'env' | 'degraded' | null`（**只增字段，既有 `present` / `label` /
 * `detail` / `diagnostic` 与既有分支语义逐字不变**）：
 *   * `'env'`      = **模型未启用**这一族 —— `raw === 'no_model'`，以及「后端没写 `degraded`、
 *                    但 `runtimeMode` 明确不属于模型驱动档」那条分支（判据继续复用
 *                    `roles.isModelDriven`，**不另造第二套**）。环境状态，交给段① 呈现。
 *   * `'degraded'` = **真的模型调用失败 / 降级** —— `provider_degraded_to_mock` /
 *                    `exception:` 前缀 / 未登记的其它非空值；代码执行面降级同属此类。
 *   * `null`       = `present === false`。
 */
export function deriveDegradeNotice(
  llm: Record<string, unknown> | undefined | null,
  runtimeMode?: string | null,
  codeplane?: Record<string, unknown> | null,
): DegradeNotice {
  // INC25 / T05 —— 代码执行面的降级**优先**（更具体的一手事实，§7）：仅当本次是代码任务
  // 且 `codeplane.degraded` 为非空字符串时命中；普通 run 的 `codeplane` 为空 ⇒ 直接落到
  // 下方既有逻辑，行为与既有调用**逐字一致**（两个新入参均为可选，向后兼容）。
  const cpNotice = codeplaneDegradeNotice(codeplane)
  if (cpNotice) return cpNotice
  const raw = llm?.degraded
  // 缺失 / 非字符串 / 空串 ⇒ 先看**档位**：离线编排档也要如实说明「未启用模型驱动」。
  if (typeof raw !== 'string' || raw.trim() === '') {
    // 判据复用 `roles.isModelDriven`（含 `graph` 属模型驱动档的边界）。
    // ⚠️ 向后兼容（勿违）：仅当调用方明确传入非空 runtimeMode 时才走该分支；
    // 省略入参 ⇒ `present=false`（INC21 既有语义，不动）。
    const modeProvided = typeof runtimeMode === 'string' && runtimeMode.trim() !== ''
    if (modeProvided && !isModelDriven(runtimeMode, llm)) {
      return {
        present: true,
        // INC39 —— 档位判定出的「未启用模型」= 环境态，交给段① 呈现。
        kind: 'env',
        label: '本次运行未启用模型驱动',
        detail:
          '当前为离线编排档，未连接模型服务：平台只记录执行过程，不生成报告正文。启用模型服务后重新运行可获得完整交付物。',
        diagnostic: null,
      }
    }
    return { present: false, kind: null, label: '', detail: null, diagnostic: null }
  }
  if (raw === 'no_model') {
    return {
      present: true,
      // INC39 —— `no_model` = 模型未启用（环境态），交给段① 呈现。
      kind: 'env',
      label: '本次运行未启用模型驱动',
      detail: '平台以确定性编排执行，只记录过程、不生成报告正文。',
      diagnostic: null,
    }
  }
  if (raw === 'provider_degraded_to_mock') {
    return {
      present: true,
      // INC39 —— 真实模型调用失败后降级 ⇒ 保留在段④「当前阻塞」。
      kind: 'degraded',
      label: '模型调用未成功，已降级执行',
      detail: '本次由内置模拟模型接管，未生成真实报告正文。',
      diagnostic: null,
    }
  }
  if (raw.startsWith('exception:')) {
    // 工程原文**不铺可见行**：detail 置 null，原文只作 title 悬浮诊断。
    return { present: true, kind: 'degraded', label: '模型调用异常，已降级执行', detail: null, diagnostic: raw }
  }
  // 未登记的 `degraded` 值：中性说明，**不臆造**原因；原始枚举只在 hover 露出。
  return { present: true, kind: 'degraded', label: '本次运行已降级执行', detail: null, diagnostic: raw }
}

/**
 * run 级**真实墙钟**（毫秒）—— 页脚「耗时」的唯一数据源。
 *
 * 口径核实（INC24 / Q5，写清依据，**勿用别的东西顶替**）：
 *   * 后端**没有** run 级专用 duration 字段：`GET /runs/{id}` 的响应模型
 *     `forgeflow/api/hub_schemas.py::RunDetailResponse` 只有 `created_at: str`（必填）
 *     与 `completed_at: str | None`（可空）——**无**耗时段。
 *   * 前端 `RunDetail` 同口径：`frontend/src/api/client.ts::RunDetail` 的
 *     `created_at: string` + `completed_at: string | null`。
 *   * `latency_ms` **只**存在于 tool invocation / round 级（**非** run 级）——
 *     故**严禁**用 `sum(latency_ms)` 求和：含 null 的部分求和会低估，是**半诚实数字**。
 *   * 「`completed_at − created_at`」是后端**既有**的 run 级耗时口径，同源
 *     `forgeflow/evaluation/agent_metrics.py::_duration_ms(created_at, completed_at)`。
 *
 * 诚实纪律（对齐 AC-7「未测量 ≠ 0」）：仅当**两端都存在** && 两端 `Date.parse` 均成功
 * && `diff >= 0` 时返回真实毫秒；任一不满足 ⇒ 返回 `null`（调用方据此**整项省略**，
 * **绝不**写成 `0`、也**不**显示「—」——页脚不显示该项即可）。
 */
export function runWallClockMs(
  createdAt: string | null | undefined,
  completedAt: string | null | undefined,
): number | null {
  if (!createdAt || !completedAt) return null
  const start = Date.parse(createdAt)
  const end = Date.parse(completedAt)
  if (Number.isNaN(start) || Number.isNaN(end)) return null
  const diff = end - start
  if (diff < 0) return null
  return diff
}

/**
 * 业务化时长文案（**无工程词**）：`< 60s ⇒「{s} 秒」`；`>= 60s ⇒「{m} 分 {s} 秒」`。
 *
 * 词表纪律（P0-5 / §7.3）：**只出「秒 / 分」**，绝不出现 `ms` / `s` 这类工程单位字样。
 * 入参必须是一个**已确认的真实测量值**（见 `runWallClockMs` 的诚实纪律）；本函数
 * 不接收 `null`（`null` 由调用方「整项省略」，而不是渲染成「—」或 0）。
 */
export function formatRunDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000))
  if (totalSec < 60) return `${totalSec} 秒`
  const minutes = Math.floor(totalSec / 60)
  const seconds = totalSec % 60
  return `${minutes} 分 ${seconds} 秒`
}

/* ------------------------------------------------------------------------- *
 * INC25 / T05 —— 代码执行面（codeplane）的派生（纯函数，无 React，无副作用）。
 *
 * 全部只读 `GET /runs/{id}`.codeplane 的**真实字段**：时间线 / Diff / 测试结论 /
 * 审批状态。后端没写的，这里就不产生 —— 非代码任务（`codeplane` 为空）一律得到
 * 「不呈现」的空结果，绝不臆造一条时间线或一张 Diff。
 * ------------------------------------------------------------------------- */

/** The run's raw `codeplane` dict (never `undefined`; `{}` for a non-code run). */
function codeplaneOf(detail: RunDetail): Record<string, unknown> {
  const cp = detail.codeplane
  return cp && typeof cp === 'object' ? (cp as Record<string, unknown>) : {}
}

/** Whether this run drove the code-execution plane (has a non-empty `codeplane`). */
export function hasCodePlane(detail: RunDetail): boolean {
  return Object.keys(codeplaneOf(detail)).length > 0
}

/** Up to `limit` named, non-tool-id step labels (business names), joined by 、. */
function stepNames(value: unknown, limit = 6): string {
  if (!Array.isArray(value)) return ''
  const named = value
    .map((v) => (typeof v === 'string' ? v.trim() : ''))
    .filter((v) => v.length > 0 && !containsPlatformToolId(v))
  return named.slice(0, limit).join('、')
}

/**
 * INC25 / T05 (§7) —— 把 `codeplane.degraded` 映射为**面向业务**的降级说明。
 *
 * 取值为非空字符串才命中；命中时**优先于** `llm.degraded`（更具体的一手事实）。
 * 业务面只出业务表述，逐字工程原文（未登记值）只作 `title` 悬浮诊断 —— 与
 * `deriveDegradeNotice` 的 llm 分支**同一纪律**（不臆造未知原因）。
 * 返回 `null` 表示「无代码执行面降级」。
 */
function codeplaneDegradeNotice(
  codeplane: Record<string, unknown> | null | undefined,
): DegradeNotice | null {
  if (!codeplane || typeof codeplane !== 'object') return null
  const raw = codeplane.degraded
  if (typeof raw !== 'string' || raw.trim() === '') return null
  const named = stepNames(codeplane.affected_steps)
  const affected = named ? `受影响步骤：${named}。` : ''
  switch (raw) {
    case 'engine_unavailable':
      return {
        present: true,
        // INC39 —— 代码执行面降级是**真实阻塞**（不是「没连模型」的环境态），保留在段④。
        kind: 'degraded',
        label: '代码执行引擎当前不可用',
        detail: `本次未执行任何代码改动。${affected}`,
        diagnostic: null,
      }
    case 'model_unavailable':
      return {
        present: true,
        kind: 'degraded',
        label: '本机模型当前不可用',
        detail: `依赖模型的代码步骤本次未执行。${affected}`,
        diagnostic: null,
      }
    case 'timeout':
      return { present: true, kind: 'degraded', label: '代码任务执行超时已中止', detail: affected || null, diagnostic: null }
    case 'runner_crashed':
      return { present: true, kind: 'degraded', label: '代码执行引擎异常退出', detail: affected || null, diagnostic: null }
    case 'parse_failed':
      return {
        present: true,
        kind: 'degraded',
        label: '测试结果无法解析，记为未测量',
        detail: '本次未判定测试是否通过。',
        diagnostic: null,
      }
    // 未登记的 degraded 值：中性说明，**不臆造**原因；原始枚举只在 hover 露出。
    default:
      return { present: true, kind: 'degraded', label: '本次运行已降级执行', detail: null, diagnostic: raw }
  }
}

/**
 * 时间线 —— 后端 `codeplane.timeline`（标准化引擎事件）的**只读投影**。
 *
 * `status` 原样保留（由 `stepStatusToStageStatus` 统一映射）；`latencyMs` 仅保留
 * **真实正数**（未测量 ⇒ `null`，界面渲染「—」，绝不 0，AC-14）。原始 `tool` /
 * `detail` 也一并带出，但**只**由折叠的 Trace 呈现（AC-13）。
 */
export function deriveCodeTimeline(detail: RunDetail): CodeTimelineItem[] {
  const raw = codeplaneOf(detail).timeline
  if (!Array.isArray(raw)) return []
  return raw.map((entry, i) => {
    const ev = (entry ?? {}) as Record<string, unknown>
    const latency = ev.latency_ms
    return {
      seq: typeof ev.seq === 'number' ? ev.seq : i,
      ts: typeof ev.ts === 'string' ? ev.ts : '',
      phase: typeof ev.phase === 'string' ? ev.phase : '',
      kind: typeof ev.kind === 'string' ? ev.kind : '',
      status: typeof ev.status === 'string' ? ev.status : 'ok',
      tool: typeof ev.tool === 'string' ? ev.tool : '',
      label: typeof ev.label === 'string' ? ev.label : '',
      detail: typeof ev.detail === 'string' ? ev.detail : '',
      latencyMs: typeof latency === 'number' && Number.isFinite(latency) && latency > 0 ? latency : null,
    }
  })
}

/** The file path a `diff --git a/<path> b/<path>` header names (best effort). */
function diffPath(header: string): string {
  const match = /\sb\/(.+)$/.exec(header)
  return (match ? match[1] : header.replace(/^diff --git\s+/, '').trim()).trim()
}

/**
 * Parse a unified diff into per-file groups (additions / deletions / lines).
 *
 * Pure and strictly verbatim: a line is bucket'd by its leading `+` / `-` / space
 * and its text kept as-is. A diff that carries no `diff --git` header (e.g. only
 * `--- a/x` / `+++ b/x`) yields no groups — the caller then renders the raw text.
 */
function parseUnifiedDiff(text: string): CodeDiffFile[] {
  const files: CodeDiffFile[] = []
  let current: CodeDiffFile | null = null
  for (const raw of (text ?? '').split('\n')) {
    if (raw.startsWith('diff --git ')) {
      current = { path: diffPath(raw), additions: 0, deletions: 0, lines: [] }
      files.push(current)
      continue
    }
    if (!current) continue
    if (
      raw.startsWith('+++ ') ||
      raw.startsWith('--- ') ||
      raw.startsWith('index ') ||
      raw.startsWith('new file') ||
      raw.startsWith('deleted file') ||
      raw.startsWith('old mode') ||
      raw.startsWith('new mode') ||
      raw.startsWith('similarity index') ||
      raw.startsWith('rename ')
    ) {
      continue
    }
    if (raw.startsWith('@@')) {
      current.lines.push({ kind: 'ctx', text: raw })
    } else if (raw.startsWith('+')) {
      current.additions += 1
      current.lines.push({ kind: 'add', text: raw })
    } else if (raw.startsWith('-')) {
      current.deletions += 1
      current.lines.push({ kind: 'rem', text: raw })
    } else if (raw.length > 0) {
      current.lines.push({ kind: 'ctx', text: raw })
    }
  }
  return files
}

/**
 * 代码变更 —— 优先取 `codeplane.diff`，无则回落已批准的 `code_diff` 产物正文
 * （两者都是后端逐字文本）。无变更 ⇒ `present=false` 的诚实空态（AC-15）。
 */
export function deriveCodeDiff(detail: RunDetail): CodeDiff {
  const cp = codeplaneOf(detail)
  const fromCp = typeof cp.diff === 'string' ? cp.diff : ''
  const fromArtifact =
    (detail.artifacts ?? []).find((a) => a.kind === 'code_diff')?.content ?? ''
  const text = fromCp.trim() ? fromCp : fromArtifact
  return { present: text.trim().length > 0, text, files: parseUnifiedDiff(text) }
}

/**
 * 测试结论 —— 后端 `codeplane.tests` 的只读投影。
 *
 * `measured === false` ⇒ 未测量（判定权在 ForgeFlow 侧；不可解析即未测量，
 * **绝不**默认判「通过」，AC-16）。计数只在确为有限数时才采用，否则 0。
 */
export function deriveTestResult(detail: RunDetail): CodeTestResult {
  const tests = codeplaneOf(detail).tests
  const t = tests && typeof tests === 'object' ? (tests as Record<string, unknown>) : {}
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const cases = Array.isArray(t.failed_cases)
    ? t.failed_cases.filter((x): x is string => typeof x === 'string' && x.trim().length > 0)
    : []
  return {
    measured: t.measured === true,
    verdict: typeof t.verdict === 'string' && t.verdict ? t.verdict : 'unmeasured',
    passed: num(t.passed),
    failed: num(t.failed),
    errors: num(t.errors),
    failedCases: cases,
    command: typeof t.command === 'string' ? t.command : '',
  }
}

/**
 * 审批状态 —— 后端 `codeplane.approval` 的只读投影。
 *
 * 后端在「待审批」时写入完整 `approval`（status / approval_id / decided_by /
 * decided_at，见 `orchestrator.py` 的 `codeplane_meta["approval"]`）；批准后的复跑
 * 由 commit 载荷携带 `status="approved"`。缺失时**不臆断**，但若运行本身停在
 * `awaiting_approval` 则如实判为 `pending`（否则为 `''`）。
 */
export function deriveCodeApproval(detail: RunDetail): CodeApproval {
  const cp = codeplaneOf(detail)
  const raw = cp.approval
  const a = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const status = (typeof a.status === 'string' && a.status ? a.status : '') ||
    (['awaiting_approval', 'pending_approval', 'paused'].includes((detail.status ?? '').toLowerCase())
      ? 'pending'
      : '')
  return {
    status,
    approvalId: typeof a.approval_id === 'string' ? a.approval_id : '',
    decidedBy: typeof a.decided_by === 'string' ? a.decided_by : '',
    decidedAt: typeof a.decided_at === 'string' ? a.decided_at : '',
    committed: cp.committed === true,
  }
}

/**
 * 本次注入的 Skill / Memory 上下文 —— 后端 `codeplane.injected` 的只读投影。
 *
 * 只读**逐字**字段（skill 的 `id`/`version`/`name`，memory 的 `id`/`scope`）；
 * 缺失 / 非列表 ⇒ 空数组（**什么都没注入**的诚实空态），**绝不**补占位。无 `id`
 * 也无 `name` 的 skill、无 `id` 的 memory 视为噪声丢弃。
 */
export function deriveInjectedContext(detail: RunDetail): CodeInjectedContext {
  const cp = codeplaneOf(detail)
  const raw =
    cp.injected && typeof cp.injected === 'object'
      ? (cp.injected as Record<string, unknown>)
      : {}
  const obj = (v: unknown): Record<string, unknown> =>
    v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
  const skills = (Array.isArray(raw.skills) ? raw.skills : [])
    .map(obj)
    .map((s) => ({
      id: typeof s.id === 'string' ? s.id : '',
      version: typeof s.version === 'string' ? s.version : '',
      name: typeof s.name === 'string' ? s.name : '',
    }))
    .filter((s) => s.id.length > 0 || s.name.length > 0)
  const memory = (Array.isArray(raw.memory) ? raw.memory : [])
    .map(obj)
    .map((m) => ({
      id: typeof m.id === 'string' ? m.id : '',
      scope: typeof m.scope === 'string' ? m.scope : '',
    }))
    .filter((m) => m.id.length > 0)
  return { skills, memory }
}

/**
 * INC29 T02 (§6) —— 进展摘要词表（后端 `codeplane.summary` 的只读投影）。
 *
 * 只读**逐字**字段（`files_changed` / `test_command` / `passed` / `failed` /
 * `repair_rounds`）；后端未测出的字段是 `null`，这里**原样保留 `null`** ——
 * **绝不**把未测量当成 `0`（与 `latencyMs` 同一条纪律）。整个 `summary` 缺失 ⇒
 * 各字段 `null`（诚实空态），不臆造。
 */
export function deriveCodeSummary(detail: RunDetail): CodeTaskSummary {
  const raw = codeplaneOf(detail).summary
  const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const num = (v: unknown): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? v : null
  const str = (v: unknown): string | null =>
    typeof v === 'string' && v.trim() !== '' ? v : null
  return {
    filesChanged: num(s.files_changed),
    testCommand: str(s.test_command),
    passed: num(s.passed),
    failed: num(s.failed),
    repairRounds: num(s.repair_rounds),
  }
}

/**
 * 装配整个代码执行面区块（供结果层一次性挂载）。非代码任务 ⇒ `present=false`、
 * 各列表/对象均为空 —— 结果层据此**不渲染**任何代码区块。
 */
export function deriveCodePlane(detail: RunDetail): CodePlaneView {
  const cp = codeplaneOf(detail)
  const engine = cp.engine && typeof cp.engine === 'object' ? (cp.engine as Record<string, unknown>) : {}
  const workspace = cp.workspace && typeof cp.workspace === 'object' ? (cp.workspace as Record<string, unknown>) : {}
  const affected = Array.isArray(cp.affected_steps)
    ? cp.affected_steps.filter((x): x is string => typeof x === 'string')
    : []
  const completed = Array.isArray(cp.completed_steps)
    ? cp.completed_steps.filter((x): x is string => typeof x === 'string')
    : []
  return {
    present: Object.keys(cp).length > 0,
    degraded: typeof cp.degraded === 'string' && cp.degraded ? cp.degraded : null,
    engineAvailable: typeof engine.available === 'boolean' ? engine.available : null,
    engineReason: typeof engine.reason === 'string' ? engine.reason : '',
    workspaceId: typeof workspace.workspace_id === 'string' ? workspace.workspace_id : '',
    workspaceState: typeof workspace.state === 'string' ? workspace.state : '',
    timeline: deriveCodeTimeline(detail),
    diff: deriveCodeDiff(detail),
    tests: deriveTestResult(detail),
    approval: deriveCodeApproval(detail),
    summary: deriveCodeSummary(detail),
    affectedSteps: affected,
    completedSteps: completed,
    injected: deriveInjectedContext(detail),
  }
}
