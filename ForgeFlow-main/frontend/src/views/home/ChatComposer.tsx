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
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useRecentHubRuns } from '../../api/hooks'
import { useQuery } from '@tanstack/react-query'
import { hubApi } from '../../api/client'
import { IconClose, IconPaperclip, IconPlus, IconSend, IconSparkle, IconStop } from '../../components/icons'
import { ResourcePicker } from '../runs/ResourcePicker'
import { ModelStatus } from '../runs/ModelStatus'
import '../../styles/home.css'

export function ChatComposer({
  onSubmit,
  busy,
  draft,
  onDraftChange,
  canExecute,
  running,
  stopping,
  onStop,
  skillId,
  initialResourceId,
}: {
  onSubmit: (intent: string, context?: Record<string, unknown>) => Promise<boolean>
  busy: boolean
  draft: string
  onDraftChange: (value: string) => void
  canExecute: boolean
  running: boolean
  stopping: boolean
  onStop: () => void
  skillId?: string
  initialResourceId?: string
}) {
  // 附件行（规格 §3）：已选资源随任务声明为 `context.resources`（真接线，后端会解引用）。
  const [resourceIds, setResourceIds] = useState<string[]>(initialResourceId ? [initialResourceId] : [])
  const [attachOpen, setAttachOpen] = useState(false)
  const [names, setNames] = useState<Record<string, string>>({})
  const inputRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = 'auto'
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`
  }, [draft])
  // 模型只读入口的数据源：最近一次运行（真实）。没有运行过 ⇒ runId 为 null
  // ⇒ ModelStatus 显示诚实空态，不伪造模型名。
  const latest = useRecentHubRuns(1)
  const skill = useQuery({ queryKey: ['skills', 'instructions', skillId], queryFn: () => hubApi.instructionSkill(skillId!), enabled: !!skillId, retry: false })
  const initialResource = useQuery({queryKey:['resources',initialResourceId],queryFn:()=>hubApi.resource(initialResourceId!),enabled:!!initialResourceId,retry:false})
  const latestRunId = latest.data?.items?.[0]?.run_id ?? null
  const ready = canExecute && draft.trim().length > 0 && !busy && !running && (!skillId || (skill.isSuccess && skill.data.status === 'published')) && (!initialResourceId || !resourceIds.includes(initialResourceId) || initialResource.isSuccess)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!ready) return
    const context: Record<string, unknown> = {}
    if (resourceIds.length > 0) context.resources = resourceIds
    const submittedIds = [...resourceIds]
    const accepted = await onSubmit(draft.trim(), Object.keys(context).length > 0 ? context : undefined)
    if (!accepted) return
    // Clear only accepted inputs; preserve files added while awaiting admission.
    setResourceIds((current) => current.filter((id) => !submittedIds.includes(id)))
    setAttachOpen(false)
  }

  return (
    <div className="chat-composer-wrap">
      {skillId && <div className="selected-instruction-skill" data-testid="selected-instruction-skill"><IconSparkle width={16} height={16} />
        {skill.isError ? '技能不可用，请返回技能库选择' : skill.data?.name || '正在加载技能…'}
        <a href="/skills">更换技能</a>
      </div>}
      {attachOpen && canExecute && (
        <div className="hero-attach-panel" data-testid="hero-attach-panel">
          <ResourcePicker
            selectedIds={resourceIds}
            onToggle={(id, record) => {
              if (record) setNames((current) => ({ ...current, [id]: record.name }))
              setResourceIds((prev) =>
                prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
              )
            }}
            onRegistered={(record) => {
              setNames((current) => ({ ...current, [record.id]: record.name }))
              setResourceIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]))
            }}
          />
        </div>
      )}

      <form className="chat-composer" data-testid="chat-composer" onSubmit={submit}>
        {resourceIds.length > 0 && <div className="chat-selected-files" aria-label="已附加文件">
          {resourceIds.map((id) => <span className="chat-selected-file" key={id}>
            <IconPaperclip width={13} height={13} /><span>{names[id] || (id === initialResourceId ? initialResource.data?.name : '') || '已附加资源'}</span>
            <button type="button" aria-label={`移除 ${names[id] || '资源'}`} onClick={() => setResourceIds((current) => current.filter((item) => item !== id))}><IconClose width={12} height={12} /></button>
          </span>)}
        </div>}
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

          {/* INC55 缺陷② —— 优先显示最新 run 实际使用的模型；无 run 记录时回退显示
              平台**配置**的模型（`platformFallback`，带「平台配置」标签，绝不写「已连接」）。 */}
          <ModelStatus runId={latestRunId} platformFallback />
        </div>

        <div className="chat-composer-inputrow">
          <label className="sr-only" htmlFor="chat-composer-input">
            输入消息
          </label>
          <textarea
            ref={inputRef}
            rows={1}
            id="chat-composer-input"
            className="chat-composer-input"
            data-testid="chat-composer-input"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            placeholder={canExecute ? (running ? '正在处理，你可以先写下一条消息…' : '描述你想完成的工作，或附加一份文件…') : '登录后开始对话'}
            disabled={!canExecute}
            autoComplete="off"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                if (ready) event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          {running ? <button type="button" className="chat-composer-send stop" data-testid="chat-composer-stop"
            disabled={stopping || !canExecute} aria-label="停止生成" title="停止生成" onClick={onStop}>
            <IconStop width={16} height={16} />
          </button> : <button
            type="submit"
            className={`chat-composer-send${ready ? ' ready' : ''}`}
            data-testid="chat-composer-send"
            disabled={!ready}
            aria-label="发送"
            title={canExecute ? '发送' : '只读身份无法发送'}
          >
            {busy ? '…' : <IconSend width={16} height={16} />}
          </button>}
        </div>

        {!canExecute && (
          <p className="chat-composer-note" role="note">
            当前身份为只读访客，无法发起对话
          </p>
        )}
      </form>
      <p className="chat-composer-hint">{running ? '智能体正在处理 · 可随时停止' : 'Enter 发送 · Shift + Enter 换行'}<span>请核对重要内容</span></p>
    </div>
  )
}
