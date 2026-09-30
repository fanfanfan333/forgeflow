import { useEffect, useState } from 'react'
import { useApprovalsPending, useApproveMutation, useRejectMutation } from '../api/hooks'
import type { Approval } from '../api/client'
import { ErrorText } from '../components/ErrorText'

export function ApprovalsView() {
  const q = useApprovalsPending()
  const pending = q.data ?? []

  return (
    <section className="view active" data-screen-label="审批">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>审批队列</h1>
            <p className="sub">
              {q.isLoading ? '加载中…' : `${pending.length} 条待批`}
              {q.isError && (
                <span style={{ color: 'var(--red-4)', marginLeft: 8 }}>
                  · <ErrorText error={q.error} label="加载失败" />
                </span>
              )}
            </p>
          </div>
          <div className="actions">
            <button className="btn sm" disabled title="指派人筛选 —— 待后端暴露指派人字段后接入">
              指派给我
            </button>
            <button className="btn sm" disabled title="默认 —— 当前视图展示全部待批项">
              全部
            </button>
            <button className="btn sm primary" disabled title="批量批准接口尚未实现">
              批量批准 · {pending.length}
            </button>
          </div>
        </div>
      </div>
      <div className="page-body">
        {pending.length === 0 && !q.isLoading ? (
          <EmptyState />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {pending.map((a) => (
              <ApprovalCard key={a.token} approval={a} />
            ))}
          </div>
        )}
        <ActivityTable />
      </div>
    </section>
  )
}

type ActivityRow = {
  token: string
  action: string
  reviewer: string
  decision: 'approved' | 'rejected' | 'sent_back'
  decisionLabel: string
  latency: string
  when: string
}

const ACTIVITY: ActivityRow[] = [
  { token: 'apr_jK02p', action: '发送提案 — Vercel · ¥96K', reviewer: 's.chen', decision: 'approved', decisionLabel: '已批准', latency: '2 分 41 秒', when: '14 分钟前' },
  { token: 'apr_mL18p', action: '退款 — Quanta · ¥1,820', reviewer: 'j.kim', decision: 'approved', decisionLabel: '已批准', latency: '4 分 12 秒', when: '38 分钟前' },
  { token: 'apr_yT72k', action: '发送提案 — Northwind · ¥260K', reviewer: 'v.lopez', decision: 'rejected', decisionLabel: '已拒绝 · 不符合 ICP', latency: '8 分 04 秒', when: '1 小时前' },
  { token: 'apr_dW91x', action: 'Q1 记账过账', reviewer: 't.alvarez', decision: 'sent_back', decisionLabel: '已退回 · 偏差 > 5%', latency: '12 分 18 秒', when: '2 小时前' },
]

function decisionBadge(d: ActivityRow['decision'], label: string) {
  if (d === 'approved') return <span className="badge emerald">{label}</span>
  if (d === 'rejected') return <span className="badge red">{label}</span>
  return <span className="badge amber">{label}</span>
}

function ActivityTable() {
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <div className="title">审批动态 · 最近 7 天</div>
        <div className="actions">
          <span className="badge amber" style={{ fontSize: 10 }}>示例数据</span>
          <span>p50 处理时长：6 分 12 秒</span>
        </div>
      </div>
      <div className="panel-body flush">
        <table className="tbl">
          <thead>
            <tr>
              <th>令牌</th>
              <th>动作</th>
              <th>审批人</th>
              <th>决策</th>
              <th className="num">耗时</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {ACTIVITY.map((r) => (
              <tr key={r.token}>
                <td><span className="id">{r.token}</span></td>
                <td>{r.action}</td>
                <td>{r.reviewer}</td>
                <td>{decisionBadge(r.decision, r.decisionLabel)}</td>
                <td className="num">{r.latency}</td>
                <td className="text-mono text-muted">{r.when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="panel">
      <div className="panel-body" style={{ padding: 64, textAlign: 'center', color: 'var(--fg-muted)' }}>
        <p style={{ fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '.12em', textTransform: 'uppercase' }}>
          收件箱已清空
        </p>
        <p style={{ marginTop: 12, fontSize: 13 }}>
          暂无待处理审批。当工作流触发人工审批中断时，审批请求会出现在这里。
        </p>
      </div>
    </div>
  )
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const [note, setNote] = useState('')
  const approve = useApproveMutation()
  const reject = useRejectMutation()
  const pending = approve.isPending || reject.isPending
  // Reading Date.now() during render is impure (react-hooks/purity). Capture it
  // in state via a lazy initializer and tick it once a minute so the displayed
  // age stays correct without an unstable render-time read.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000)
    return () => clearInterval(id)
  }, [])
  const ageMin = Math.max(0, Math.floor((now - new Date(approval.requested_at).getTime()) / 60000))
  const proposal = approval.proposal as Record<string, unknown>
  const title = (proposal?.title as string) ?? (proposal?.subject as string) ?? `审批 · ${approval.token.slice(0, 8)}`
  const summary = (proposal?.summary as string) ?? (proposal?.description as string)

  return (
    <div className="approval">
      <div className="hd">
        <span className="badge amber">● 待处理 · {ageMin} 分</span>
        <span className="meta">
          {approval.token.slice(0, 8)} · {approval.workflow_id?.slice(0, 8) ?? '—'}
        </span>
      </div>
      <div className="ttl">{title}</div>
      {summary && <div className="meta">{summary}</div>}
      <pre className="diff" style={{ margin: '12px 0 0' }}>
        {Object.entries(proposal ?? {})
          .filter(([k]) => k !== 'title' && k !== 'summary' && k !== 'description' && k !== 'subject')
          .slice(0, 6)
          .map(([k, v]) => (
            <span key={k}>
              <span className="add">+ {k}</span>
              {'  '}
              {typeof v === 'string' ? v : JSON.stringify(v)}
              {'\n'}
            </span>
          ))}
      </pre>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="可选备注…"
        style={{
          marginTop: 10,
          width: '100%',
          padding: '6px 10px',
          background: 'var(--bg-inset)',
          border: '1px solid var(--border-subtle)',
          borderRadius: 5,
          color: 'var(--fg-primary)',
          fontFamily: 'var(--font-mono)',
          fontSize: 11.5,
          outline: 'none',
        }}
      />
      <div className="ctas">
        <button
          className="btn sm primary"
          style={{ flex: 1, justifyContent: 'center' }}
          disabled={pending}
          title="批准该步骤并恢复工作流"
          onClick={() => approve.mutate({ token: approval.token, note })}
        >
          {approve.isPending ? '批准中…' : '批准并恢复'}
        </button>
        <button
          className="btn sm"
          style={{ flex: 1, justifyContent: 'center' }}
          disabled={pending}
          title="拒绝该步骤。此决策为最终决定，不可撤销。"
          onClick={() => reject.mutate({ token: approval.token, note })}
        >
          {reject.isPending ? '拒绝中…' : '拒绝'}
        </button>
      </div>
      <p style={{ margin: '8px 0 0', fontSize: 11, color: 'var(--fg-muted)' }}>
        批准后将从该检查点恢复运行；拒绝为最终决定，不可撤销。
      </p>
      {(approve.isError || reject.isError) && (
        <div style={{ color: 'var(--red-4)', fontSize: 11, marginTop: 8 }}>
          <ErrorText error={approve.error ?? reject.error} label="操作失败" />
        </div>
      )}
    </div>
  )
}
