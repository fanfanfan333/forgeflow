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

/**
 * The preset follow-up instructions for the quick actions (INC18-B).
 *
 * Each one really creates a NEW run through the same `POST /tasks` path the
 * 继续执行 box uses — these are not decorative buttons. They say what they do.
 */
export const QUICK_ACTIONS: { key: string; label: string; instruction: string }[] = [
  { key: 'exec-summary', label: '生成管理层摘要', instruction: '基于上面的结果，生成一份面向管理层的摘要（不超过 200 字，只保留结论与建议）' },
  { key: 'anomaly', label: '分析异常指标', instruction: '对上面结果里的异常指标做归因分析，指出可能原因与验证方法' },
  { key: 'compare', label: '对比上一周期', instruction: '将上面的结果与上一周期做对比，列出增长与下滑的项' },
  { key: 'follow-up', label: '创建下一周跟踪任务', instruction: '基于上面的结论，列出下一周需要跟踪的指标与负责人' },
]

/**
 * INC24 / P1-5 —— 「让 Agent 处理」在**完成态**提交的默认后续指令（业务化、无工程词）。
 *
 * 与 `QUICK_ACTIONS` 同款纪律：它会被**真的提交**（`POST /tasks`，`onContinue` 通路），
 * 不是装饰文案。它同时用于 agent 动作的 `title` 预览（「Agent 将执行：{文本}」）。
 *
 * 词表纪律（P0-5）：**不得**含工具 id / 工程枚举 / `tool` / `prompt` / `step_id` 等术语。
 */
export const AGENT_RESUME_INSTRUCTION =
  '基于本次运行的结果继续推进：补齐尚缺的必要信息，完成计划中剩余的步骤，并给出最终交付内容。'

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
