/**
 * InlineSessionPanel — 首页「就地展开」的内联流式会话面板（INC-INLINE-STREAMING / T04）。
 *
 * 组件树（D10：插在 Hero 下方、与「近期任务」共存）：
 *   conv-inline-panel(data-run-id)
 *    ├─ header: conv-inline-header / conv-inline-stop / conv-inline-close
 *    ├─ conv-inline-user            ← useRunDetail(runId).intent
 *    ├─ <WorkspaceLiveStrip ... />  ← 复用既有步骤条（受控 props）
 *    ├─ <InlineAnswer ... />        ← 打字机答案（纯展示 deltas）
 *    ├─ conv-inline-aborted         ← 中断时逐字「已中断，以下内容不完整」
 *    ├─ conv-inline-open-full       ← 跳到**已存在**的 /tasks/$runId 深链（F1）
 *    ├─ conv-inline-error           ← 停止 / 详情失败的诚实说明
 *    └─ <FollowUpComposer ... />    ← 复用追问框（conv-inline-followup，AC-10）
 *
 * 关键纪律：
 *   · **单 run 单 SSE**：本组件**只调一次** `useRunEvents(runId)`，把 `events/done/error`
 *     传 `WorkspaceLiveStrip`、`deltas/streaming` 传 `InlineAnswer`（两个子组件**都不订阅**）。
 *   · `conv-inline-stop` **仅在 `running && canExecute` 时渲染**（否则**不渲染**该节点，不是
 *     disabled 死按钮，AC-11）。
 *   · `conv-inline-aborted` 触发条件 = `deltas.interrupted === true` **或** `events` 中出现
 *     `run.aborted` **任一**（AC-12）；文案**逐字**「已中断，以下内容不完整」。
 *   · F1 —— 结果口径 = **只做「最终答案」+ 一个跳到完整视图的链接**；**绝不**内嵌 `ResultPanel`
 *     （那需要约 30 个 props 与首页不该有的第二份派生逻辑 ⇒ 正是 B3 的病根）。产物 / 证据 /
 *     成本 / 执行轨迹全在 `/tasks/$runId` 深链那边。
 *
 * data-testid（新增 10 个）：见上。
 */
import { useAbortRun, useRunDetail, useWorkspaceCreateTask } from '../../api/hooks'
import type { RunHandle } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { useSession } from '../../hooks/useSession'
import { useRunEvents } from '../../hooks/useRunEvents'
import { roleConfigFor } from '../../home/roleConfig'
import { WorkspaceLiveStrip } from './WorkspaceLiveStrip'
import { InlineAnswer } from './InlineAnswer'
import { FollowUpComposer } from './FollowUpComposer'

/** run 的终态词表（与后端终态一致）——非终态即「运行中」。 */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'aborted', 'interrupted', 'rejected'])

export function InlineSessionPanel({
  runId,
  onClose,
  onFollowUp,
}: {
  /** 要展示的运行 id（由 `HomeView` 组件态持有，**不入 URL**，C7）。 */
  runId: string
  /** 关闭面板（`HomeView` 置 `activeRunId = null`，回到首页初始态）。 */
  onClose: () => void
  /** 追问成功后回传新句柄，供父级把面板切到新 run。 */
  onFollowUp?: (handle: RunHandle) => void
}) {
  // ★ 单条 SSE：唯一订阅点。子组件一律走受控 props（不各自订阅）。
  const { events, deltas, done, error } = useRunEvents(runId)
  const detail = useRunDetail(runId)
  const session = useSession()
  const canExecute = roleConfigFor(session?.role).canExecute
  const abort = useAbortRun()
  const create = useWorkspaceCreateTask()

  const status = detail.data?.status ?? ''
  // FD-5 —— 运行中 = 未收尾（SSE 未 done）且非终态；状态未知（detail 未加载 / 查询失败）时
  // 以 `deltas.streaming` 作**阳性证据**兜底（历史回放 / 已完成档 streaming===false ⇒ 不会误闪）。
  const statusKnown = status !== ''
  const running = !done && (statusKnown ? !TERMINAL_STATUSES.has(status) : deltas.streaming)

  // AC-12 / FD-1 —— 中断横幅判据**终态优先**，避免「完整答案 + 已中断，以下内容不完整」自相矛盾：
  //   · 总线出现 `run.aborted` / `run.failed` ⇒ 确为异常终态；
  //   · `run.completed`（或 status==='completed'）⇒ 已完成，**不再**显示中断横幅
  //     （真模型流内异常 → 后端降级到确定性执行器 → run **正常完成**并给完整答案的情形）；
  //   · 仅当半截 turn 标记为 interrupted **且未完成**时才据其显示。
  const terminalBad = events.some((e) => e.type === 'run.aborted' || e.type === 'run.failed')
  const completed = events.some((e) => e.type === 'run.completed') || status === 'completed'
  const aborted = terminalBad || (deltas.interrupted === true && !completed)

  const intent = detail.data?.intent ?? ''

  const stopError = abort.error ? humanizeError(abort.error, '停止任务失败') : null
  const detailError = detail.isError ? humanizeError(detail.error, '运行详情加载失败') : null
  const errorLabel = stopError?.label ?? detailError?.label ?? null

  // 追问：真调 `POST /workspace/tasks` 并带 `parent_run_id = runId`（AC-10）；成功后切到新 run。
  const onContinue = (nextInstruction: string, context: Record<string, unknown>) => {
    create.mutate(
      { intent: nextInstruction, context, parentRunId: runId },
      { onSuccess: (handle) => onFollowUp?.(handle) },
    )
  }
  const followUpError = create.error ? humanizeError(create.error, '继续对话失败').label : null

  return (
    <section
      className="conv-inline-panel"
      data-testid="conv-inline-panel"
      data-run-id={runId}
      aria-label="内联会话"
    >
      <header className="conv-inline-head" data-testid="conv-inline-header">
        <span className="conv-inline-title">实时会话</span>
        <div className="conv-inline-actions">
          {/* AC-11：仅运行中 + 可执行身份才渲染停止；否则该节点**不渲染**（不是 disabled）。 */}
          {running && canExecute && (
            <button
              type="button"
              className="btn sm danger"
              data-testid="conv-inline-stop"
              onClick={() => {
                if (!abort.isPending) abort.mutate(runId)
              }}
              disabled={abort.isPending}
            >
              {abort.isPending ? '停止中…' : '停止任务'}
            </button>
          )}
          <button
            type="button"
            className="btn sm"
            data-testid="conv-inline-close"
            onClick={onClose}
          >
            关闭
          </button>
        </div>
      </header>

      {intent && (
        <p className="conv-inline-user" data-testid="conv-inline-user">
          {intent}
        </p>
      )}

      {/* 既有步骤条（受控）——「执行过程」。 */}
      <WorkspaceLiveStrip events={events} done={done} error={error} mode="concise" />

      {/* 打字机最终答案（纯展示 deltas；无文本时不渲染节点）。 */}
      <InlineAnswer deltas={deltas} streaming={deltas.streaming} />

      {aborted && (
        <p className="conv-inline-aborted" data-testid="conv-inline-aborted" role="note">
          已中断，以下内容不完整
        </p>
      )}

      {/* F1 —— 跳到**已存在**的 `/tasks/$runId` 深链；产物 / 证据 / 成本 / 轨迹在那里。 */}
      <a
        className="conv-inline-open-full"
        data-testid="conv-inline-open-full"
        href={`/tasks/${runId}`}
      >
        在完整视图中打开
      </a>

      {errorLabel && (
        <p className="conv-inline-error" data-testid="conv-inline-error" role="alert">
          {errorLabel}
        </p>
      )}

      {/* AC-10 —— 复用追问框；新 testid 由**外层容器**承载（`FollowUpComposer` 的
          `data-testid="conv-followup"` 字面量必须逐字保留，见 FD-3 项目红线）。 */}
      <div className="conv-inline-followup" data-testid="conv-inline-followup">
        <FollowUpComposer
          runId={runId}
          onContinue={onContinue}
          pending={create.isPending}
          error={followUpError}
          inputId="conv-inline-followup-input"
        />
      </div>
    </section>
  )
}
