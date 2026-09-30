/**
 * INC24 / 段⑤「下一步动作」—— 「下一步」必须是**动作**，不是建议（P0-4）。
 *
 * 三档动作（语义显式声明，**白名单外不放任何按钮**，见 AC-5 / §7.5）：
 *   * `result-action-agent`     —— **真调后端**：有阻塞/无交付时走 `onRerun`
 *                                 （`POST /runs/{id}/replan`）；完成态走 `onAgent`
 *                                 （`POST /tasks`）。点击后**必有**一次真实请求。
 *   * `result-action-self`      —— **不调后端**：`edit.startEdit()` 进入既有本地编辑态
 *                                 （可见变化：`result-ctas` 内 `result-edit` 消失、`result-save` 出现）。
 *   * `result-action-view-diff` —— **不调后端**：`onViewDiff()` → `onTabChange('trace')`
 *                                 （可见变化：`#res-panel-trace` 变为可见）。
 *
 * 前半（首屏）= 「下一步：A → B → C」+ 三档动作 + `result-continue`（用户模板里的 `[继续执行]`
 * 主 CTA，其 helper 长句已移入 `title`）+ `result-save-note`（保存反馈，见下）；后半 = 对结果的
 * 操作（`result-quick` / `result-ctas` / 4 条 note：export / store / print / copy），**整组收进
 * 默认折叠的「更多操作」`<details>`**（压降首屏「行动区」字数以达 AC-8）。
 *
 * ⚠️ `result-save-note`（保存 / 编辑反馈）**不在这组折叠内**、恒可见 —— 原因见下方该元素的注释
 * （`saveEdit` 的成功/未修改路径会 `setEditing(false)` ⇒ 抽屉合上，若留在内则反馈文字消失）。
 *
 * ⚠️ 无产物（`hasArtifact === false`）时：**隐藏** `self` 档、`result-save-note` 与整个「更多操作」
 * 折叠组（无编辑目标 ⇒ 点击无可见变化 ⇒ 会是 AC-5 判定的「假按钮」；见设计 Q-E）。此时段⑤
 * 仍有 `agent` + `view-diff` + 自定义指令入口 ⇒ 容器**恒有内容**。
 */
import { AGENT_RESUME_INSTRUCTION } from './resultActions'
import type { ArtifactEdit } from './useArtifactEdit'
import type { NextActionsDerivation } from './types'

export function ResultNextActions({
  derivation,
  canRerun,
  onAgent,
  agentPending,
  onViewDiff,
  edit,
  hasArtifact,
  quickActions,
  onQuickAction,
  continueValue,
  onContinueValue,
  onSubmitContinue,
  continuePending,
  continueError,
  runId,
}: {
  derivation: NextActionsDerivation
  /** agent 档走哪条后端通路（true ⇒ replan；false ⇒ 创建后续任务）。 */
  canRerun: boolean
  /** agent 档点击回调（由 `ResultPanel` 依 `canRerun` 选通路，**都真调后端**）。 */
  onAgent: () => void
  /** agent 档进行中（replan 或创建任务 pending）。 */
  agentPending: boolean
  /** view-diff 档：切「执行轨迹」Tab（**不调**后端）。 */
  onViewDiff: () => void
  /** 共享编辑态（self 档 + `result-ctas` 共用）。 */
  edit: ArtifactEdit
  /** 是否存在可编辑的产物（无产物 ⇒ 隐藏 self 与 ctas）。 */
  hasArtifact: boolean
  /** 后续动作（`QUICK_ACTIONS`）：每一条都真调后端（`POST /tasks`）。 */
  quickActions: { key: string; label: string; instruction: string }[]
  onQuickAction: (instruction: string) => void
  continueValue: string
  onContinueValue: (value: string) => void
  onSubmitContinue: () => void
  continuePending: boolean
  continueError?: string | null
  runId: string
}) {
  // P1-5 —— agent 档的指令预览（业务化，无工程词）。有阻塞 ⇒ 如实说「重新运行并继续未完成步骤」。
  const agentTitle = canRerun
    ? '智能体将基于现有声明重新运行，并继续未完成的步骤'
    : `智能体将执行：${AGENT_RESUME_INSTRUCTION}`

  return (
    <section className="res-next" data-testid="result-next-actions" aria-label="下一步动作">
      <h4 className="res-sect-head">下一步</h4>

      {/* 「下一步：A → B → C」——真实状态派生（受阻步骤优先 + 未执行步骤），空则不渲染。 */}
      {derivation.steps.length > 0 && (
        <p className="res-next-line">下一步：{derivation.steps.join(' → ')}</p>
      )}

      {/* 三档动作：agent（真调后端）/ self（进编辑态，不调）/ view-diff（切 Tab，不调）。 */}
      <div className="res-action-row">
        <button
          type="button"
          className="btn sm primary"
          data-testid="result-action-agent"
          onClick={onAgent}
          disabled={agentPending}
          title={agentTitle}
        >
          {agentPending ? '处理中…' : '让智能体处理'}
        </button>
        {hasArtifact && (
          <button
            type="button"
            className="btn sm"
            data-testid="result-action-self"
            onClick={edit.startEdit}
          >
            我自己处理
          </button>
        )}
        <button
          type="button"
          className="btn sm ghost"
          data-testid="result-action-view-diff"
          onClick={onViewDiff}
        >
          查看变更
        </button>
      </div>

      {/* 自定义指令 → 创建一次后续运行（真调后端 POST /tasks）。
          这段（`result-continue`）**留在首屏** —— 它就是用户模板里的 `[继续执行]` 主 CTA。
          其 helper 长句搬迁到 `title`，首屏可见文本收敛为「提交后会创建一个新运行」，
          以压降「行动区」字数（AC-8），信息一条不删（全文在 `title` 里可读）。 */}
      <div className="res-continue">
        <label className="res-continue-label" htmlFor={`res-continue-${runId}`}>
          继续执行
        </label>
        <div className="res-continue-row">
          <input
            id={`res-continue-${runId}`}
            className="res-continue-input"
            value={continueValue}
            onChange={(e) => onContinueValue(e.target.value)}
            placeholder="输入下一步指令，例如：把线索按行业分层再出结论"
            disabled={continuePending}
            autoComplete="off"
          />
          <button
            type="button"
            className="btn sm primary"
            data-testid="result-continue"
            onClick={onSubmitContinue}
            disabled={continuePending || !continueValue.trim()}
          >
            {continuePending ? '提交中…' : '继续执行'}
          </button>
        </div>
        <p
          className="res-subtle"
          title="继续执行会创建一个新运行，并在后端记录它与本运行的父子关系；上一轮上下文会注入新运行的规划。"
        >
          提交后会创建一个新运行
        </p>
        {continueError && (
          <p className="res-error" role="alert">
            {continueError}
          </p>
        )}
      </div>

      {/* 保存 / 编辑反馈（`result-save-note`）：**恒在折叠之外、始终可见**。
          ⚠️ 可用性（防功能断裂，勿再挪回折叠内）：`useArtifactEdit.ts::saveEdit` 的两条路径
          ——「内容未修改」与「已保存」——都会 `setEditing(false)`，`.res-more` 的 `key` 随之回到
          `'idle'` 并**折叠**；若这句留在折叠体内，用户清空正文或点保存后将**看不到任何文字反馈**
          （界面像坏了）。故把它移出 `.res-more-body`，紧随「继续执行」块之后、`<details>` 之前。
          间距由 `.res-next` 的 `gap` 统一提供（它是 `.res-next` 的**直接子节点**）。
          本 `<p>` **不在任何 `<details>` 子树内**（`closest('details') === null`）；
          testid / `title` 全句 / 无操作占位文案均与 E4 一致，未新增或改动 testid。 */}
      {hasArtifact && (
        <p
          className="res-note"
          data-testid="result-save-note"
          title="结果编辑仅在本地生效（未写回服务端）"
        >
          {edit.saveNote ?? '编辑仅在本地生效'}
        </p>
      )}

      {/* ⑤ 后半：对结果的操作（quick / ctas / 各 note）——**整组**收进默认折叠的「更多操作」。
          首屏因此只留「下一步：…」+ 三档动作 +「继续执行」主 CTA，压降「行动区」字数（AC-8）。
          ⚠️ 折叠纪律（保留，勿删；与 runs.css 同款口径）：`.res-more-body` **不写 `display`**。
          依据（Chrome 153 实测，CDP）：折叠态隐藏由 UA 的 `::details-content { content-visibility:
          hidden }` 承担，**任何后代 `display` 都穿不透它** —— 故在当前引擎上，即便把
          `display:flex` 的 `.res-quick` / `.res-ctas` **直接**挂在 `<details>` 下也**不会**泄漏
          （实测 `checkVisibility() === false`）；本 wrapper 纪律因此**作为跨引擎 / 旧引擎防御保留**：
          旧契约把隐藏写成 UA 的 `details:not([open]) > * { display:none }`，那时 author 的
          `display` 会覆盖它 ⇒ 直接子节点带 `display` 才会静默泄漏。
          证据出处：`qa_tmp/inc24/QA-INC24-REPORT.md` §3 对照②
          （`inc24_foldprobe.json` / `inc24_foldleak.json`）。
          ⚠️ 可用性（防功能断裂）：`edit.editing === true` 时**强制 `open`** 并 `key` 重挂载 ——
          点「我自己处理」进编辑态后，`result-ctas` 内的 保存/取消 立即可见，不会因折叠而藏起来；
          `key` 重挂载同时避免「受控 open」与用户手动折叠互相打架。
          （保存反馈 `result-save-note` 已**外移**到本次折叠之外恒可见，见上方注释，勿再挪回。）
          ⚠️ 不给本 `<details>` 加 data-testid（本轮新增 testid 固定为设计 §7.1 的 10 个）。 */}
      {hasArtifact && (
        <details className="res-more" key={edit.editing ? 'editing' : 'idle'} open={edit.editing}>
          <summary className="res-more-sum">
            更多操作（导出 / 复制 / 打印 / 存入知识库 / 编辑）
          </summary>
          <div className="res-more-body">
            {!edit.editing && (
              <div className="res-quick" data-testid="result-quick" aria-label="后续动作">
                <span className="res-quick-label">继续交给智能体</span>
                <div className="res-quick-row">
                  {quickActions.map((a) => (
                    <button
                      type="button"
                      className="btn sm ghost"
                      key={a.key}
                      data-testid={`result-quick-${a.key}`}
                      onClick={() => onQuickAction(a.instruction)}
                      disabled={continuePending}
                      title={a.instruction}
                    >
                      {a.label}
                    </button>
                  ))}
                </div>
              </div>
            )}

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

            {/* 用户可见说明：可见文本收敛为一句，全句（含「包含平台执行账本」「上方读视图仅展示
                交付部分」）原样移入 `title` —— **信息一条不删**。 */}
            <p
              className="res-note"
              data-testid="result-export-note"
              title="导出 / 复制 / 打印的是完整原文（包含平台执行账本）；上方读视图仅展示交付部分。"
            >
              导出 / 复制 / 打印的是完整原文
            </p>
            {/* 导出后的可见确认（下载本身无网络请求；无此确认则「导出」在 AC-5 口径下像死按钮）。
                不新增 testid（本轮新增 testid 清单固定为设计 §7.1 的 10 个）。 */}
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
      )}
    </section>
  )
}
