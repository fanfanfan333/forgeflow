/**
 * INC39 / 段⑤「上下文快捷操作」+ 结果操作（替代 INC24 的 `ResultNextActions`）。
 *
 * 病灶（修复对象）：段⑤ 过去固定渲染三个按钮（「让智能体处理」/「我自己处理」/「查看变更」）
 * + 一个重复的续聊输入框，让页面看起来像**任务审批 / 工作流系统**，而不是 Agent。
 *
 * 现在：
 *   * **不再**渲染「下一步」标题、**不再**渲染三档固定按钮、**不再**渲染任何输入框；
 *   * 只按当前任务的**真实产物**动态渲染 **0～3** 条**上下文快捷操作**
 *     （`deriveContextualActions`，规则见 `resultActions.ts`）：每条都**真实可执行**；
 *   * `actions.length === 0` ⇒ `result-contextual-actions` 整段**不进 DOM**（不留空壳、
 *     不写「无后续动作」话术）；
 *   * 续聊统一到中列底部的 `FollowUpComposer`（`conv-followup`，ChatGPT 的 composer 位），
 *     段⑤ **不再**有输入框（`result-continue` 退役）。
 *
 * 结果操作（编辑 / 导出 / 复制 / 打印 / 存入知识库）**保留**（testid 不删不改），
 * 收进默认折叠的「更多操作」`<details>`，以维持「极简首屏」。折叠纪律与既有同款：
 * `.res-more-body` **不写 `display`**（见 runs.css）；`edit.editing === true` 时强制 `open`
 * 并 `key` 重挂载 —— 进编辑态后 保存/取消 立即可见可点。
 *
 * data-testid：新增 `result-contextual-actions` / `result-ctx-<key>`；
 * 保留 `result-ctas` / `result-edit|save|cancel|export|copy|print|store` /
 * `result-export-note` / `result-store-note` / `result-print-note` / `result-copy-note` /
 * `result-save-note`（一个都不删或改名）。
 */
import type { ArtifactEdit } from './useArtifactEdit'
import type { ContextAction, ContextActionInput, RunTab } from './types'
import { deriveContextualActions } from './resultActions'
import { scrollToId } from './CodeTaskTimeline'

export function ResultContextActions({
  context,
  onContinue,
  continuePending,
  onRerun,
  rerunPending,
  onTabChange,
  onExport,
  edit,
  hasArtifact,
}: {
  /** 已派生的真实业务上下文（计数均为已渲染列表的 `.length`）。 */
  context: ContextActionInput
  /** `continue` 档：真调 `POST /workspace/tasks`（带 `parent_run_id`）。 */
  onContinue: (instruction: string) => void
  /** `continue` 档进行中（按钮禁用）。 */
  continuePending: boolean
  /** `rerun` 档：真调 `POST /runs/{id}/replan`。 */
  onRerun: () => void
  /** `rerun` 档进行中（按钮禁用）。 */
  rerunPending: boolean
  /** `sources` / `trace` 档：切换 Tab（真实可见态变化）。 */
  onTabChange: (tab: RunTab) => void
  /** `export` 档：纯前端下载完整原文（`useArtifactEdit.exportResult`）。 */
  onExport: () => void
  /** 共享编辑态（段⑤ 的 编辑/保存/取消 与段⑥ 编辑器共用）。 */
  edit: ArtifactEdit
  /** 是否存在可操作的产物（无产物 ⇒ 隐藏「更多操作」折叠组）。 */
  hasArtifact: boolean
}) {
  const actions = deriveContextualActions(context)

  // 每条动作的执行机制（**都真有效果**，无装饰按钮）。`diff` 复用既有 `#code-diff`。
  const run = (a: ContextAction) => {
    switch (a.kind) {
      case 'continue':
        if (a.instruction) onContinue(a.instruction)
        return
      case 'rerun':
        onRerun()
        return
      case 'diff':
        scrollToId(document, 'code-diff')
        return
      case 'export':
        onExport()
        return
      case 'sources':
        onTabChange('evidence')
        return
      case 'trace':
        onTabChange('trace')
        return
    }
  }

  const showContextual = actions.length > 0
  const showMore = hasArtifact
  // 无上下文动作且无产物 ⇒ 整段不进 DOM（不渲染空壳）。
  if (!showContextual && !showMore) return null

  return (
    <div className="res-ctx-region">
      {/* 上下文快捷操作：仅当确有 0～3 条真实动作时渲染。 */}
      {showContextual && (
        <section className="res-ctx" data-testid="result-contextual-actions" aria-label="快捷操作">
          {actions.map((a) => (
            <button
              key={a.key}
              type="button"
              className="btn sm"
              data-testid={a.testid}
              title={a.instruction}
              onClick={() => run(a)}
              disabled={
                a.kind === 'continue' ? continuePending : a.kind === 'rerun' ? rerunPending : false
              }
            >
              {a.label}
            </button>
          ))}
        </section>
      )}

      {/* 结果操作（保留，testid 不删不改）：恒可见的保存/编辑反馈 + 默认折叠的「更多操作」。 */}
      {showMore && (
        <>
          <p
            className="res-note"
            data-testid="result-save-note"
            title="结果编辑仅在本地生效（未写回服务端）"
          >
            {edit.saveNote ?? '编辑仅在本地生效'}
          </p>
          <details className="res-more" key={edit.editing ? 'editing' : 'idle'} open={edit.editing}>
            <summary className="res-more-sum">
              更多操作（导出 / 复制 / 打印 / 存入知识库 / 编辑）
            </summary>
            <div className="res-more-body">
              <div className="res-ctas" data-testid="result-ctas">
                {!edit.editing && (
                  <button
                    type="button"
                    className="btn sm"
                    data-testid="result-edit"
                    onClick={edit.startEdit}
                  >
                    编辑
                  </button>
                )}
                {edit.editing && (
                  <>
                    <button
                      type="button"
                      className="btn sm primary"
                      data-testid="result-save"
                      onClick={edit.saveEdit}
                    >
                      保存
                    </button>
                    <button
                      type="button"
                      className="btn sm"
                      data-testid="result-cancel"
                      onClick={edit.cancelEdit}
                    >
                      取消
                    </button>
                  </>
                )}
                {!edit.editing && (
                  <button
                    type="button"
                    className="btn sm"
                    data-testid="result-export"
                    onClick={edit.exportResult}
                  >
                    导出 Markdown
                  </button>
                )}
                {!edit.editing && (
                  <button
                    type="button"
                    className="btn sm"
                    data-testid="result-copy"
                    onClick={edit.copyResult}
                  >
                    复制全文
                  </button>
                )}
                {!edit.editing && (
                  <button
                    type="button"
                    className="btn sm"
                    data-testid="result-print"
                    onClick={edit.exportPdf}
                  >
                    导出 PDF
                  </button>
                )}
                {!edit.editing && (
                  <button
                    type="button"
                    className="btn sm"
                    data-testid="result-store"
                    onClick={edit.saveToMemory}
                    disabled={edit.storePending}
                  >
                    {edit.storePending ? '存入中…' : '存入知识库'}
                  </button>
                )}
              </div>

              {/* 用户可见说明：可见文本收敛为一句，全句原样移入 `title`（信息一条不删）。 */}
              <p
                className="res-note"
                data-testid="result-export-note"
                title="导出 / 复制 / 打印的是完整原文（包含平台执行账本）；上方读视图仅展示交付部分。"
              >
                导出 / 复制 / 打印的是完整原文
              </p>
              {/* 导出后的可见确认（下载本身无网络请求；无此确认则「导出」像死按钮）。 */}
              {edit.exportNote && (
                <p className="res-note" role="status">
                  {edit.exportNote}
                </p>
              )}
              {edit.storeNote && (
                <p className="res-note" data-testid="result-store-note" role="status">
                  {edit.storeNote}
                </p>
              )}
              {edit.printNote && (
                <p className="res-note" data-testid="result-print-note" role="status">
                  {edit.printNote}
                </p>
              )}
              {edit.copyNote && (
                <p className="res-note" data-testid="result-copy-note" role="status">
                  {edit.copyNote}
                </p>
              )}
            </div>
          </details>
        </>
      )}
    </div>
  )
}
