/**
 * KnowledgeView — 知识库 (Memory Hub view, PRD §6.2 / T17).
 *
 * Surfaces the five-layer memory scopes, the scoped memory entries, and the
 * experiences produced by the closed loop (经验 = 参数化知识资产).
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { useExperiences, useMemoryList, useMemoryScopes } from '../api/hooks'
import { hubApi } from '../api/client'
import type { Experience, MemoryEntry, MemoryScopeInfo } from '../api/client'
import { useQueryClient, useMutation } from '@tanstack/react-query'
import '../styles/skills.css'

export function KnowledgeView() {
  return (
    <section className="view active" data-screen-label="知识库">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>知识库</h1>
            <p className="sub">
              五层记忆 + 任务经验沉淀 · 数据来自 <span className="mono">/api/memory</span> 与{' '}
              <span className="mono">/api/experiences</span>
            </p>
          </div>
          <div className="actions">
            <a className="btn sm" href="/skills">前往技能中心 →</a>
          </div>
        </div>
      </div>
      <div className="page-body hub">
        <ScopeSection />
        <MemorySection />
        <ExperienceSection />
      </div>
    </section>
  )
}

function ScopeSection() {
  const q = useMemoryScopes()
  const scopes = q.data ?? []
  return (
    <div style={{ marginBottom: 20 }}>
      <div className="sec-head">
        <div>
          <div className="sec-title">五层记忆模型</div>
          <div className="sec-sub">用户 · 团队 · 情景 · 语义 · 组织</div>
        </div>
      </div>
      {q.isLoading ? (
        <div className="scope-grid">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="card scope-card"><div className="skel" style={{ height: 56 }} /></div>
          ))}
        </div>
      ) : (
        <div className="scope-grid">
          {scopes.map((s) => (
            <ScopeCard key={s.scope} scope={s} />
          ))}
        </div>
      )}
    </div>
  )
}

function ScopeCard({ scope }: { scope: MemoryScopeInfo }) {
  return (
    <div className="card scope-card">
      <span className="sc-badge badge blue">{scope.scope}</span>
      <span className="sc-label">{scope.label}</span>
      <span className="sc-desc">{scope.description}</span>
      <span className="sc-rules">
        写 {scope.writable_by.join('/') || '—'} · 读 {scope.readable_by.join('/') || '—'}
        {scope.promotable ? ' · 可下沉' : ''}
      </span>
    </div>
  )
}

function MemorySection() {
  const [scope, setScope] = useState('')
  const listQ = useMemoryList({ scope: scope || undefined, limit: 50 })
  const items = listQ.data?.items ?? []
  return (
    <div className="panel" style={{ marginBottom: 20 }}>
      <div className="panel-head">
        <div className="title">记忆条目</div>
        <div className="actions">
          <select
            className="hub-select"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            aria-label="按层级筛选"
          >
            <option value="">全部层级</option>
            <option value="user">user</option>
            <option value="team">team</option>
            <option value="episodic">episodic</option>
            <option value="semantic">semantic</option>
            <option value="org">org</option>
          </select>
          <span>{listQ.isLoading ? '加载中…' : `${items.length} 条`}</span>
        </div>
      </div>
      <WriteMemory inline />
      <div className="panel-body flush">
        {items.length === 0 && !listQ.isLoading ? (
          <div className="empty"><span className="big">≋</span>该层级暂无记忆</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>层级</th>
                <th>内容</th>
                <th>命名空间</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <MemoryRow key={m.id} entry={m} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function MemoryRow({ entry }: { entry: MemoryEntry }) {
  return (
    <tr>
      <td><span className="badge blue">{entry.scope}</span></td>
      <td style={{ maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {entry.content}
      </td>
      <td className="text-mono text-muted text-12">{entry.namespace}</td>
      <td className="num text-mono text-muted text-12">{relativeTime(entry.created_at)}</td>
    </tr>
  )
}

function WriteMemory({ inline }: { inline?: boolean }) {
  const [content, setContent] = useState('')
  const [scope, setScope] = useState('semantic')
  const qc = useQueryClient()
  const create = useMutation({
    mutationFn: () => hubApi.createMemory({ scope, content: content.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory'] })
      setContent('')
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!content.trim() || create.isPending) return
    create.mutate()
  }

  return (
    <form
      className="hub-toolbar"
      onSubmit={submit}
      style={{ padding: '12px 16px', margin: 0, borderBottom: '1px solid var(--border-subtle)' }}
    >
      <select className="hub-select" value={scope} onChange={(e) => setScope(e.target.value)} aria-label="写入层级">
        <option value="user">user</option>
        <option value="team">team</option>
        <option value="episodic">episodic</option>
        <option value="semantic">semantic</option>
        <option value="org">org</option>
      </select>
      <label className="hub-search" style={{ maxWidth: 'none' }}>
        <input
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="写入一条记忆（提交前会经过 DLP 扫描）…"
          aria-label="记忆内容"
        />
      </label>
      <button type="submit" className="btn sm primary" disabled={create.isPending || !content.trim()}>
        {create.isPending ? '写入中…' : inline ? '写入记忆' : '写入'}
      </button>
    </form>
  )
}

function ExperienceSection() {
  const q = useExperiences({ limit: 30 })
  const items = q.data?.items ?? []
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">任务经验</div>
        <div className="actions">
          <span>{q.isLoading ? '加载中…' : `${q.data?.total ?? 0} 条`}</span>
        </div>
      </div>
      <div className="panel-body flush">
        {items.length === 0 && !q.isLoading ? (
          <div className="empty">
            <span className="big">◔</span>暂无经验，发起一个任务后会自动沉淀
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>经验</th>
                <th>结果</th>
                <th>标签</th>
                <th>来源 Run</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <ExperienceRow key={e.id} exp={e} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function ExperienceRow({ exp }: { exp: Experience }) {
  const ok = exp.outcome === 'success'
  return (
    <tr>
      <td style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        <span className="name">{exp.summary}</span>
      </td>
      <td>
        <span className={`badge ${ok ? 'emerald' : 'amber'}`}>{ok ? '成功' : exp.outcome}</span>
      </td>
      <td className="text-12 text-muted">{exp.tags.join(', ') || '—'}</td>
      <td className="id">{exp.run_id.slice(0, 10)}</td>
      <td className="num text-mono text-muted text-12">{relativeTime(exp.created_at)}</td>
    </tr>
  )
}

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const diff = Math.max(0, Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}
