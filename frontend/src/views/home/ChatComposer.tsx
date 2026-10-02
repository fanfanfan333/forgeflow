/**
 * ChatComposer — 首页底部富输入区（INC43 / T01，P0-9）。
 *
 * 结构（对齐截图界面一底部）：
 *   `chat-composer`
 *    ├─ 工具行：`chat-composer-attach`(📎 添加文件) / `chat-composer-skill`(✨ Skill)
 *    │          / `chat-composer-tools`(+ 工具) + 既有 `ModelStatus`
 *    └─ 输入行：`chat-composer-input` + `chat-composer-send`
 *
 * testid 兼容（硬约束）：旧 `Hero` 的附件行 / 附件面板被本组件取代，其四个**字面量**
 * 必须继续存在 —— 承载映射：
 *   · `chat-composer-attach` ↔ `hero-attach-files`（均打开 `ResourcePicker`）；
 *   · `chat-composer-skill`  ↔ `hero-attach-skills`（均指向技能中心）；
 *   · `chat-composer-tools`  ↔ `hero-attach-more`（`<details>` 菜单）；
 *   · `hero-attach-panel`     = 附件面板容器（复用既有 class）。
 *
 * 诚实纪律：附件选择复用既有 `ResourcePicker`（真调 `/resources*`）；提交复用
 * `useChatThread.submit` → `POST /workspace/tasks`（**不跳页**）。
 */
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useRecentHubRuns } from '../../api/hooks'
import { IconPaperclip, IconPlus, IconSend, IconSparkle } from '../../components/icons'
import { ResourcePicker } from '../runs/ResourcePicker'
import { ModelStatus } from '../runs/ModelStatus'
import '../../styles/home.css'

export function ChatComposer({
  onSubmit,
  busy,
  draft,
  onDraftChange,
  canExecute,
}: {
  onSubmit: (intent: string, context?: Record<string, unknown>) => void
  busy: boolean
  draft: string
  onDraftChange: (value: string) => void
  canExecute: boolean
}) {
  // 附件行（规格 §3）：已选资源随任务声明为 `context.resources`（真接线，后端会解引用）。
  const [resourceIds, setResourceIds] = useState<string[]>([])
  const [attachOpen, setAttachOpen] = useState(false)
  // 模型只读入口的数据源：最近一次运行（真实）。没有运行过 ⇒ runId 为 null
  // ⇒ ModelStatus 显示诚实空态，不伪造模型名。
  const latest = useRecentHubRuns(1)
  const latestRunId = latest.data?.items?.[0]?.run_id ?? null
  const ready = canExecute && draft.trim().length > 0 && !busy

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!ready) return
    const context: Record<string, unknown> = {}
    if (resourceIds.length > 0) context.resources = resourceIds
    onSubmit(draft.trim(), Object.keys(context).length > 0 ? context : undefined)
    // 提交后清空已选附件（草稿由 `useChatThread` 负责清空）。
    setResourceIds([])
    setAttachOpen(false)
  }

  return (
    <div className="chat-composer-wrap">
      {attachOpen && canExecute && (
        <div className="hero-attach-panel" data-testid="hero-attach-panel">
          <ResourcePicker
            selectedIds={resourceIds}
            onToggle={(id) =>
              setResourceIds((prev) =>
                prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
              )
            }
            onRegistered={(record) =>
              setResourceIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]))
            }
          />
        </div>
      )}

      <form className="chat-composer" data-testid="chat-composer" onSubmit={submit}>
        <div className="chat-composer-tools">
          <button
            type="button"
            className={`chat-composer-tool${attachOpen ? ' on' : ''}`}
            data-testid="chat-composer-attach"
            aria-expanded={attachOpen}
            disabled={!canExecute}
            onClick={() => setAttachOpen((v) => !v)}
          >
            <IconPaperclip width={14} height={14} />
            <span data-testid="hero-attach-files">添加文件</span>
            {resourceIds.length > 0 && <span className="chat-composer-count">{resourceIds.length}</span>}
          </button>

          <a className="chat-composer-tool" href="/skills" data-testid="chat-composer-skill">
            <IconSparkle width={14} height={14} />
            <span data-testid="hero-attach-skills">Skill</span>
          </a>

          <details className="chat-composer-more" data-testid="chat-composer-tools">
            <summary className="chat-composer-tool">
              <IconPlus width={14} height={14} />
              <span data-testid="hero-attach-more">工具</span>
            </summary>
            <div className="chat-composer-menu">
              <a href="/tasks">继续历史对话</a>
              <a href="/knowledge">知识库</a>
              <a href="/skills">技能中心</a>
            </div>
          </details>

          <ModelStatus runId={latestRunId} />
        </div>

        <div className="chat-composer-inputrow">
          <label className="sr-only" htmlFor="chat-composer-input">
            输入消息
          </label>
          <input
            id="chat-composer-input"
            className="chat-composer-input"
            data-testid="chat-composer-input"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            placeholder={canExecute ? '输入消息…' : '当前身份为只读，无法发起对话'}
            disabled={!canExecute}
            autoComplete="off"
          />
          <button
            type="submit"
            className={`chat-composer-send${ready ? ' ready' : ''}`}
            data-testid="chat-composer-send"
            disabled={!ready}
            aria-label="发送"
            title={canExecute ? '发送' : '只读身份无法发送'}
          >
            {busy ? '…' : <IconSend width={16} height={16} />}
          </button>
        </div>

        {!canExecute && (
          <p className="chat-composer-note" role="note">
            当前身份为只读访客，无法发起对话
          </p>
        )}
      </form>
    </div>
  )
}
