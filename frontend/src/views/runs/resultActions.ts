/**
 * INC14 — result-layer actions (纯函数 / 无 React / 无副作用).
 *
 * Exported separately from the component so the filename rule, the "primary
 * artifact" rule and the pure-frontend download are testable and reusable
 * without mounting a component tree. Nothing here talks to the backend: the
 * export is a local `Blob` + `<a download>` click (design §4.3) — there is
 * deliberately NO server-side export endpoint, and this module must not imply
 * one.
 */
import type { RunArtifact } from '../../api/client'
import type { ContextAction, ContextActionInput } from './types'

/**
 * The 主产物 — the **last** artifact (the final `report.render` step's output is
 * this run's finished deliverable). Returns `null` for a run with no artifact,
 * so callers degrade to the honest empty state.
 */
export function pickPrimaryArtifact(artifacts: RunArtifact[]): RunArtifact | null {
  return artifacts.length > 0 ? artifacts[artifacts.length - 1] : null
}

/** The download filename for a run's result, e.g. `run-<id>-result.md`. */
export function resultMarkdownFilename(runId: string): string {
  return `run-${runId}-result.md`
}

/**
 * Save `text` to the user's machine as `filename` — purely client-side.
 *
 * Uses a `Blob` + an object URL on a transient `<a download>`; the URL is
 * revoked immediately. No request is made to the server (设计 §4.3: 导出是纯
 * 前端行为).
 */
/**
 * Copy `text` to the clipboard (INC18 — 一个**真的能用**的结果动作).
 *
 * Returns whether it really happened; the caller reports a failure honestly
 * instead of claiming success. When the browser exposes no clipboard API (or
 * denies it) this returns `false` — we deliberately do NOT fake a "copied"
 * toast, and we deliberately do NOT implement actions the platform cannot back
 * (发送 / 推送 / 建知识库文档): a button that cannot work is worse than no
 * button, so this module grows an action only when it can really be performed.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Clipboard denied / unavailable — reported honestly by the caller.
  }
  return false
}

/**
 * 导出 PDF — via the browser's own print-to-PDF (INC18-B).
 *
 * Deliberately NOT a new dependency: `window.print()` hands the page to the
 * user's print dialog, where "另存为 PDF" is a native target. Introducing a
 * client-side PDF library for this would be a heavy, un-audited dependency;
 * faking a `.pdf` download of Markdown bytes would be a lie about the format.
 */
export function printResult(): boolean {
  if (typeof window === 'undefined' || typeof window.print !== 'function') return false
  window.print()
  return true
}

/* ------------------------------------------------------------------------- *
 * INC39 —— 结果层段⑤「上下文快捷操作」的派生（纯函数，可脱离 payload 单测）。
 *
 * 取代 INC18-B 写死的 `QUICK_ACTIONS`（4 条固定项）与 INC24 的「恒三档」动作：
 * 动作**不再写死**，而是由当前任务的**真实产物**动态派生 **0～3** 条。派生只吃
 * **已派生**的业务值（`ContextActionInput`，见 types.ts），不摸原始 `RunDetail`，
 * 不臆造、不翻译、不默认成 payload 里没有的东西。
 *
 * ⚠️ 每条动作都**真实可执行**（严禁装饰按钮，机制见各 `kind`）：
 *   * `continue` → `onContinue(instruction)`（真调 `POST /workspace/tasks`，带 `parent_run_id`）；
 *   * `diff`     → 滚动到真实代码变更（既有 `#code-diff`）—— 仅当 `codeplane.diff.present`；
 *   * `export`   → `edit.exportResult()`（真实本地下载 + 既有导出确认文案）—— 仅当有产物；
 *   * `sources`  → 切到「证据」Tab（真实可见态变化）—— 仅当确有来源；
 *   * `trace`    → 切到「执行轨迹」Tab（真实可见态变化）。
 *
 * 选档规则（**按序判定、互斥**，先命中即定档，最多 3 条；不满足前置条件的一律不进列表）：
 *   1. **代码档** `codeplane.present === true`
 *        ⇒ 「继续修复」(continue) / 「查看差异」(diff，仅 `diff.present`)。
 *        ⚠️ 红线：`diff.present === false` 时**绝不**出现任何 Diff 动作。
 *   2. **数据档** `metrics > 0 || findings > 0`
 *        ⇒ 「继续深入分析」(continue) / 「生成图表」(continue) / 「导出报告」(export，仅 `artifacts > 0`)。
 *   3. **知识档** `sources > 0`（且非代码档）
 *        ⇒ 「继续追问」(continue) / 「查看来源」(sources) / 「生成摘要」(continue)。
 *   4. **兜底** `artifacts > 0`
 *        ⇒ 「导出报告」(export) + 「查看执行过程」(trace)。
 *   5. 以上皆不满足 ⇒ **返回空数组**（段⑤ 整段不进 DOM，不留空壳）。
 *
 * ⚠️ **去重规则（INC39 收敛，勿加回）**：段⑤ **只提供别处没有的入口** —— 与同屏
 * **非折叠**入口（段④「当前阻塞」/ 段①「执行环境状态」/ 代码执行面区块）**同义且同守卫**的
 * 动作，一律**不进列表**。依据（rerun 存在性推导）：`canRerun = missingInputs > 0 || !hasDeliverable`；
 * ① `missingInputs > 0`、② `!hasDeliverable` 且无降级、③ `!hasDeliverable` 且 `degrade.kind='degraded'`
 * 三种情形段④ **必然渲染**并给出 `result-rerun`；④ `!hasDeliverable` 且 `degrade.kind='env'` 时段①
 * 的 `ResultEnvStatus` 已给 `result-env-rerun` ⇒ **只要 rerun 真可用，段④ 或段① 就必有且只有一个
 * 入口**。故「重新运行」不再出现在段⑤（原 `result-ctx-code-rerun` 已随本轮收敛移除）。
 *
 * 词表纪律（P0-5）：每条 `label` / `instruction` 均为**业务语**，**不得**含 `prompt` /
 * `tool` / `step_id` / `model` / `token` / `llm` / `runtime_mode` / `degrade` 等工程术语。
 * ------------------------------------------------------------------------- */

/** 「继续修复」提交的业务指令（代码档 continue）——业务化、无工程词。 */
const INSTRUCTION_FIX = '继续修复本次运行中尚未解决的问题，并确认改动可用。'
/** 「继续深入分析」提交的业务指令（数据档 continue）。 */
const INSTRUCTION_DEEP_DIVE = '基于本次结果继续深入分析：指出关键指标的变化趋势与可能原因。'
/** 「生成图表」提交的业务指令（数据档 continue）。 */
const INSTRUCTION_CHART = '把本次结果里的关键指标整理成清晰的图表。'
/** 「继续追问」提交的业务指令（知识档 continue）。 */
const INSTRUCTION_FOLLOW_UP = '基于本次检索到的资料继续追问，补充更需要确认的细节。'
/** 「生成摘要」提交的业务指令（知识档 continue）。 */
const INSTRUCTION_SUMMARY = '把本次结果整理成一份简明摘要。'

/** testid = `result-ctx-` + key（稳定、可预测，供 QA 逐条断言）。 */
function ctxAction(key: string, label: string, kind: ContextAction['kind'], instruction?: string): ContextAction {
  return { key, label, kind, testid: `result-ctx-${key}`, ...(instruction ? { instruction } : {}) }
}

/**
 * 段⑤「上下文快捷操作」派生（纯函数）。返回 **0～3** 条，规则见文件顶部注释。
 */
export function deriveContextualActions(ctx: ContextActionInput): ContextAction[] {
  const out: ContextAction[] = []
  const push = (a: ContextAction) => {
    if (out.length < 3) out.push(a)
  }

  // 1) 代码档
  if (ctx.codeplane.present) {
    push(ctxAction('code-fix', '继续修复', 'continue', INSTRUCTION_FIX))
    // 红线：没有真实变更文本 ⇒ **不出现**「查看差异」。
    if (ctx.codeplane.diff.present) {
      push(ctxAction('code-diff', '查看差异', 'diff'))
    }
    // ⚠️ 去重规则：**不**在此产出「重新运行」——段④ `result-rerun` / 段① `result-env-rerun`
    // 已专责该入口（同守卫、同回调），段⑤ 再加一个必然重复（见文件顶部「去重规则」）。
    return out
  }

  // 2) 数据档
  if (ctx.metrics > 0 || ctx.findings > 0) {
    push(ctxAction('data-deep-dive', '继续深入分析', 'continue', INSTRUCTION_DEEP_DIVE))
    push(ctxAction('data-chart', '生成图表', 'continue', INSTRUCTION_CHART))
    if (ctx.artifacts > 0) {
      push(ctxAction('data-export', '导出报告', 'export'))
    }
    return out
  }

  // 3) 知识档
  if (ctx.sources > 0) {
    push(ctxAction('kb-follow-up', '继续追问', 'continue', INSTRUCTION_FOLLOW_UP))
    push(ctxAction('kb-sources', '查看来源', 'sources'))
    push(ctxAction('kb-summary', '生成摘要', 'continue', INSTRUCTION_SUMMARY))
    return out
  }

  // 4) 兜底
  if (ctx.artifacts > 0) {
    push(ctxAction('fallback-export', '导出报告', 'export'))
    push(ctxAction('fallback-trace', '查看执行过程', 'trace'))
    return out
  }

  // 5) 无任何特征产物 ⇒ 空数组（段⑤ 整段不进 DOM）。
  return out
}

export function downloadTextFile(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}
