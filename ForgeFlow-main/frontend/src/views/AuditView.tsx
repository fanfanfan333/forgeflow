import { useState } from 'react'
import { useAuditSearch, useAuditStats } from '../api/hooks'
import type { AuditRow } from '../api/client'

export function AuditView() {
  const [actionFilter, setActionFilter] = useState<string | undefined>(undefined)
  const stats = useAuditStats(7)
  const search = useAuditSearch(actionFilter)

  return (
    <section className="view active" data-screen-label="审计">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>审计日志</h1>
            <p className="sub">
              不可篡改 · 按租户 + 日期分区 ·{' '}
              {/* /audit/stats degrades to {error} on a query failure — never crash on it. */}
              {stats.data?.total != null
                ? `${stats.data.total.toLocaleString()} 条记录` +
                  (typeof stats.data.window_days === 'number' ? `（${stats.data.window_days} 天）` : '')
                : '—'}
              {stats.data?.denied != null && ` · ${stats.data.denied} 条拒绝`}
              {stats.data?.errors != null && ` · ${stats.data.errors} 条错误`}
              {stats.data?.error && (
                <span style={{ color: 'var(--danger-fg)' }}> · 统计不可用：{stats.data.error.slice(0, 80)}</span>
              )}
            </p>
          </div>
          <div className="actions">
            <button className={`btn sm${!actionFilter ? ' primary' : ''}`} onClick={() => setActionFilter(undefined)}>
              全部
            </button>
            <button className={`btn sm${actionFilter === 'GET' ? ' primary' : ''}`} onClick={() => setActionFilter('GET')}>
              GET
            </button>
            <button className={`btn sm${actionFilter === 'POST' ? ' primary' : ''}`} onClick={() => setActionFilter('POST')}>
              POST
            </button>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="panel">
          <div className="panel-head">
            <div className="title">近期事件</div>
            <div className="actions">
              <span className="mono">前 50 条</span>
              <span style={{ color: 'var(--fg-faint)' }}>·</span>
              <span>{search.data?.items ? `${search.data.items.length} / ${search.data.total}` : '—'}</span>
              {search.data?.error && (
                <span style={{ color: 'var(--danger-fg)' }}>· {search.data.error.slice(0, 80)}</span>
              )}
            </div>
          </div>
          <div className="panel-body flush">
            <div
              className="audit-row"
              style={{
                background: 'var(--bg-page)',
                borderBottom: '1px solid var(--border-default)',
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                letterSpacing: '.12em',
                textTransform: 'uppercase',
                color: 'var(--fg-muted)',
              }}
            >
              <span>时间</span>
              <span>操作者</span>
              <span>动作</span>
              <span>作用域</span>
              <span style={{ textAlign: 'right' }}>IP</span>
            </div>
            {search.isLoading && (
              <div style={{ padding: 24, textAlign: 'center', color: 'var(--fg-muted)' }}>加载中…</div>
            )}
            {search.data?.items?.length === 0 && !search.isLoading && (
              <div style={{ padding: 24, textAlign: 'center', color: 'var(--fg-muted)' }}>
                没有匹配的记录。
              </div>
            )}
            {(search.data?.items ?? []).map((row) => (
              <AuditRowEl key={row.id} row={row} />
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

function timeOnly(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

function actionColor(action: string | null): string {
  if (!action) return 'var(--fg-muted)'
  if (action === 'GET') return 'var(--fg-accent)'
  if (action === 'POST') return 'var(--amber-4)'
  if (action === 'PUT' || action === 'PATCH') return 'var(--purple-4)'
  if (action === 'DELETE') return 'var(--danger-fg)'
  return 'var(--fg-primary)'
}

function AuditRowEl({ row }: { row: AuditRow }) {
  const ip = (row.metadata?.client_ip as string | undefined) ?? '—'
  return (
    <div className="audit-row">
      <span className="when">{timeOnly(row.timestamp)}</span>
      {/* Audit actor is an evidence field — echo the backend's real value
          ("anonymous", see forgeflow/api/hub_schemas.py::actor_user_id and the
          8 middleware/router sites) rather than rewriting it. */}
      <span className="actor">{row.user_id ?? 'anonymous'}</span>
      <span className="action">
        <span style={{ color: actionColor(row.action) }}>{row.action ?? '—'}</span>{' '}
        {row.resource ?? '—'}
      </span>
      <span className="scope">{row.role ?? ''}</span>
      <span className="ip">{ip}</span>
    </div>
  )
}
