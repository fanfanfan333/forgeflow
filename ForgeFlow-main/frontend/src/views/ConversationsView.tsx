import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useWorkspaceSessions } from '../api/hooks'
import { useSession } from '../hooks/useSession'
import { humanizeError } from '../api/errors'
import { DeleteConversation } from '../components/DeleteConversation'

export function ConversationsView() {
  const session = useSession()
  const sessions = useWorkspaceSessions(100, !!session)
  const [search, setSearch] = useState('')
  const items = (sessions.data?.items ?? []).filter(item => item.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  return <section className="conversations-page" data-testid="conversations-page">
    <header><h1>最近对话</h1><p>继续你的工作，或回顾之前的想法。</p></header>
    <input className="conversation-search" type="search" placeholder="搜索对话" aria-label="搜索历史对话" value={search} onChange={e => setSearch(e.target.value)} />
    {sessions.isLoading && session && <p className="conversation-empty">正在加载…</p>}
    {sessions.isError && <p role="alert">{humanizeError(sessions.error, '历史加载失败').label} <button className="chat-text-action" onClick={() => void sessions.refetch()}>重试</button></p>}
    {!sessions.isError && !sessions.isLoading && !items.length && <p className="conversation-empty">{!session ? '登录后查看历史对话' : search ? '没有找到匹配的对话' : '还没有对话，开始聊聊吧。'}</p>}
    <div className="conversation-list">{items.map(item => <div key={item.session_id} className="conversation-row">
      <Link to={'/chat/' + encodeURIComponent(item.session_id)} className="conversation-open">
        <span className="conversation-title">{item.title || '未命名对话'}</span>
        <span className="conversation-meta">{new Date(item.created_at).toLocaleDateString('zh-CN')} · {item.run_count} 轮对话</span>
      </Link>
      <DeleteConversation sessionId={item.session_id} title={item.title || '未命名对话'} />
    </div>)}</div>
    <Link to="/" className="conversation-start">开始对话 ↗</Link>
    <Link to={'/runs' as string} className="conversation-start">查看详细运行记录 ↗</Link>
  </section>
}
