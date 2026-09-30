/**
 * SecurityView — 安全与权限 (Security Hub, PRD §6.3 / T17).
 *
 * Aggregates isolation / DLP / policy state, lists policies, and resolves the
 * HITL approvals raised by the governance gate.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { useDecideApproval, useHubApprovals, usePolicies, useSecurityOverview } from '../api/hooks'
import { hubApi } from '../api/client'
import type { HubApproval, Policy } from '../api/client'
import { useMutation } from '@tanstack/react-query'
import { IconCheck } from '../components/icons'
import '../styles/skills.css'

export function SecurityView() {
  return (
    <section className="view active" data-screen-label="安全与权限">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>安全与权限</h1>
            <p className="sub">
              多租户隔离 · DLP · 策略引擎 · 数据来自安全概览接口
            </p>
          </div>
        </div>
      </div>
      <div className="page-body hub">
        <OverviewTiles />
        <ApprovalsPanel />
        <PoliciesPanel />
        <PolicyEvaluator />
      </div>
    </section>
  )
}

function OverviewTiles() {
  const q = useSecurityOverview()
  const s = q.data
  if (q.isLoading) {
    return (
      <div className="sec-grid">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="card sec-tile"><div className="skel" style={{ height: 48 }} /></div>
        ))}
      </div>
    )
  }
  if (!s) return <div className="card empty">无法加载安全概览</div>
  return (
    <div className="sec-grid">
      <div className="card sec-tile">
        <span className="sv" style={{ color: 'var(--emerald-4)' }}>{s.status}</span>
        <span className="sl">系统状态</span>
      </div>
      <div className="card sec-tile">
        <span className="sv">{s.isolation_level}</span>
        <span className="sl">租户隔离级别</span>
      </div>
      <div className="card sec-tile">
        <span className="sv">{s.dlp_enabled ? '已启用' : '关闭'}</span>
        <span className="sl">DLP 数据防泄漏</span>
      </div>
      <div className="card sec-tile">
        <span className="sv">{s.encryption} · {s.access_control}</span>
        <span className="sl">加密 / 权限控制</span>
      </div>
    </div>
  )
}

function ApprovalsPanel() {
  const q = useHubApprovals({ status: 'pending' })
  const decide = useDecideApproval()
  const items = q.data?.items ?? []
  return (
    <div className="panel" style={{ marginBottom: 20 }}>
      <div className="panel-head">
        <div className="title">待处理审批 (HITL)</div>
        <div className="actions">
          <span>{q.isLoading ? '加载中…' : `${items.length} 条待批`}</span>
        </div>
      </div>
      <div className="panel-body flush">
        {items.length === 0 && !q.isLoading ? (
          /* INC34 轮2 — SVG 对勾替换 `✓` 文本符号。 */
          <div className="empty"><span className="big"><IconCheck width={22} height={22} /></span>暂无待处理审批</div>
        ) : (
          items.map((a) => <ApprovalRow key={a.id} approval={a} onDecide={decide} />)
        )}
      </div>
      {decide.isError && (
        <p className="text-12" style={{ color: 'var(--red-4)', padding: '0 16px 12px' }}>
          操作失败：{(decide.error as Error)?.message}
        </p>
      )}
    </div>
  )
}

function ApprovalRow({
  approval,
  onDecide,
}: {
  approval: HubApproval
  onDecide: ReturnType<typeof useDecideApproval>
}) {
  const riskTone = approval.risk_level === 'high' ? 'red' : approval.risk_level === 'medium' ? 'amber' : 'blue'
  return (
    <div className="approval-row">
      <span className={`badge ${riskTone}`}>{approval.risk_level}</span>
      <div className="ap-main">
        <div className="ap-action">{approval.requested_action}</div>
        <div className="ap-meta">
          发起人 {approval.requester ?? '—'} · {relativeTime(approval.created_at)}
          {approval.note ? ` · ${approval.note}` : ''}
        </div>
      </div>
      <button
        type="button"
        className="btn sm"
        disabled={onDecide.isPending}
        onClick={() => onDecide.mutate({ approvalId: approval.id, decision: 'reject' })}
      >
        拒绝
      </button>
      <button
        type="button"
        className="btn sm primary"
        disabled={onDecide.isPending}
        onClick={() => onDecide.mutate({ approvalId: approval.id, decision: 'approve' })}
      >
        批准
      </button>
    </div>
  )
}

function PoliciesPanel() {
  const q = usePolicies()
  const items = q.data?.items ?? []
  return (
    <div className="panel" style={{ marginBottom: 20 }}>
      <div className="panel-head">
        <div className="title">策略清单</div>
        <div className="actions">
          <span>{q.isLoading ? '加载中…' : `${items.length} 条策略`}</span>
        </div>
      </div>
      <div className="panel-body flush">
        {items.length === 0 && !q.isLoading ? (
          <div className="empty"><span className="big">ᛝ</span>暂无策略</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>主体</th>
                <th>资源</th>
                <th>动作</th>
                <th>效果</th>
                <th>描述</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <PolicyRow key={p.id} policy={p} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function PolicyRow({ policy }: { policy: Policy }) {
  return (
    <tr>
      <td className="text-mono text-12">{policy.subject}</td>
      <td className="text-mono text-12">{policy.resource}</td>
      <td className="text-mono text-12">{policy.action}</td>
      <td>
        <span className={`badge ${policy.effect === 'allow' ? 'emerald' : policy.effect === 'deny' ? 'red' : 'amber'}`}>
          {policy.effect}
        </span>
      </td>
      <td>{policy.description || '—'}</td>
    </tr>
  )
}

function PolicyEvaluator() {
  const [subject, setSubject] = useState('sales_rep')
  const [resource, setResource] = useState('skills')
  const [action, setAction] = useState('write')
  const evaluate = useMutation({
    mutationFn: () => hubApi.evaluatePolicy({ subject, resource, action }),
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    evaluate.mutate()
  }

  const d = evaluate.data
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">策略决策试算</div>
      </div>
      <form className="hub-toolbar" onSubmit={submit} style={{ padding: '12px 16px', margin: 0 }}>
        <input className="hub-select" value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="主体" />
        <input className="hub-select" value={resource} onChange={(e) => setResource(e.target.value)} aria-label="资源" />
        <input className="hub-select" value={action} onChange={(e) => setAction(e.target.value)} aria-label="动作" />
        <button type="submit" className="btn sm primary" disabled={evaluate.isPending}>
          {evaluate.isPending ? '计算中…' : '试算'}
        </button>
      </form>
      {d && (
        <div className="panel-body" style={{ paddingTop: 0 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            <span className={`badge ${d.effect === 'allow' ? 'emerald' : 'red'}`}>{d.effect}</span>
            <span className="badge amber">风险 {d.risk_level}</span>
            {d.requires_approval && <span className="badge amber">需人工审批</span>}
            <span className="text-12 text-muted">{d.reason}</span>
          </div>
        </div>
      )}
      {evaluate.isError && (
        <p className="text-12" style={{ color: 'var(--red-4)', padding: '0 16px 12px' }}>
          试算失败：{(evaluate.error as Error)?.message}
        </p>
      )}
    </div>
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
