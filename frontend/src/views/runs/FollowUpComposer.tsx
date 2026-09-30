/**
 * FollowUpComposer — INC36 L1 ⑥「底部 follow-up 输入框」（对话主入口）。
 *
 * 用户在下方的输入框里继续交代目标，提交即**真调后端**：经 `LiveRunsView.onContinue`
 * → `useWorkspaceCreateTask`（`POST /workspace/tasks`）并带 `parent_run_id = 当前 run_id`。
 * 因此 follow-up 不是前端假拼接 —— 父子关系由**真实请求参数**承载（AC-39）。
 *
 * 与 L3 的关系：L3「下一步动作」里既有的 `result-continue` 等 `result-quick-*` **保留不动**
 * （归档用途）；本组件是**主入口**，二者并存。
 *
 * 诚实纪律：提交失败经 `humanizeError` **如实展示**，绝不假装成功；提交中禁用防重。
 *
 * data-testid（只增不改不删）：`conv-followup`。
 */
import { useState } from 'react'
import type { FormEvent } from 'react'

export function FollowUpComposer({
  runId,
  onContinue,
  pending,
  error,
}: {
  /** 当前 run id —— 作为 follow-up 的 `parent_run_id` 与上下文来源。 */
  runId: string
  /** 复用既有 `onContinue`（内部真调 `POST /workspace/tasks`）。 */
  onContinue: (nextInstruction: string, context: Record<string, unknown>) => void
  /** 提交中（禁用输入与按钮）。 */
  pending: boolean
  /** 提交失败的诚实说明（可空）。 */
  error: string | null
}) {
  // 以 `runId` 门控输入（换 run 自动清空，不跨 run 泄漏 —— 与 `ResultNextActions` 同法）。
  const [state, setState] = useState<{ runId: string; value: string }>({ runId, value: '' })
  const value = state.runId === runId ? state.value : ''
  const ready = value.trim().length > 0 && !pending

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const text = value.trim()
    if (!text || pending) return
    onContinue(text, { continued_from_run_id: runId })
    setState({ runId, value: '' })
  }

  return (
    <form className="conv-followup" data-testid="conv-followup" onSubmit={submit}>
      <label className="sr-only" htmlFor="conv-followup-input">
        继续对话
      </label>
      <input
        id="conv-followup-input"
        value={value}
        onChange={(e) => setState({ runId, value: e.target.value })}
        placeholder="继续告诉 AI 你想做什么…"
        disabled={pending}
        autoComplete="off"
      />
      <button type="submit" className="btn primary" disabled={!ready}>
        {pending ? '发送中…' : '发送'}
      </button>
      {error && (
        <span className="conv-followup-error" role="alert">
          {error}
        </span>
      )}
    </form>
  )
}
