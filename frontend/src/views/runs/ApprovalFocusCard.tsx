/**
 * 人工审核 focus card (design §7) — the single strong visual anchor of the page.
 *
 * HONESTY (§7.5): the approval shown is demo content for the demo run. Action
 * buttons always hit the *real* decision endpoint (via `useDecideApproval`) and
 * surface a humanised error on failure — they never fake success. When a real
 * pending approval exists it is targeted by id; otherwise the demo token is used
 * and a failure is reported as-is.
 */
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { useDecideApproval, useHubApprovals } from '../../api/hooks'
import { useSession } from '../../hooks/useSession'
import { openSignIn } from '../../components/authEvents'
import type { RunApproval, ViewMode } from './types'

const MAX_COLLAPSED = 3

/** risk_level → badge tone — identical mapping to `SecurityView` (G1). */
function riskTone(level: string | undefined): 'red' | 'amber' | 'blue' {
  return level === 'high' ? 'red' : level === 'medium' ? 'amber' : 'blue'
}

function diffLineClass(line: string): string {
  if (line.startsWith('+')) return 'diff-line add'
  if (line.startsWith('-')) return 'diff-line rem'
  return 'diff-line'
}

export function ApprovalFocusCard({ approval, mode }: { approval: RunApproval; mode: ViewMode }) {
  const session = useSession()
  const hub = useHubApprovals({ status: 'pending' })
  const decide = useDecideApproval()
  const real = hub.data?.items?.[0]

  const [previewOpen, setPreviewOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | undefined>(undefined)
  const [info, setInfo] = useState<string | null>(null)

  const collapsed = mode !== 'debug' && !previewOpen
  const shown = collapsed ? approval.preview.slice(0, MAX_COLLAPSED) : approval.preview
  const canCollapse = mode !== 'debug' && approval.preview.length > MAX_COLLAPSED

  const actionId = real?.id ?? approval.approvalToken
  const tone = riskTone(real?.risk_level)
  const disabled = !session || decide.isPending

  const reset = () => {
    setError(null)
    setErrorDetail(undefined)
    setInfo(null)
  }

  const runDecision = async (decision: 'approve' | 'reject') => {
    reset()
    try {
      await decide.mutateAsync({ approvalId: actionId, decision })
      setInfo(decision === 'approve' ? '审批已提交。' : '已提交拒绝。')
    } catch (err) {
      const h = humanizeError(err, '审批失败')
      if (err instanceof ApiError && err.status === 401) {
        setError('会话已过期，请重新登录后再试。')
      } else if (err instanceof ApiError && err.status === 403) {
        setError('当前角色无法审批（viewer 为只读）。请以 manager 身份登录。')
      } else {
        setError(h.label)
      }
      setErrorDetail(h.detail)
    }
  }

  return (
    <section className="approval-focus" aria-label="人工审核">
      <div className="af-head">
        <span className={`badge ${real ? tone : 'amber'}`}>
          <span className="dot" style={{ background: real ? `var(--${tone}-4)` : 'var(--amber-4)' }} /> 需要审批
        </span>
        <span className="af-meta">
          {real ? '真实待办' : '示例'} · {actionId}
          {approval.assignee ? ` · 指派给 ${approval.assignee}` : ''}
        </span>
      </div>

      <div className="af-title">{real?.requested_action ?? approval.title}</div>

      <div className="af-scores">
        评分 {approval.score.toFixed(1)}/10 · 符合 ICP（{approval.icpFit}）· {approval.riskCount} 项风险标记
        {real ? ` · 风险等级 ${real.risk_level}` : ''}
      </div>

      <pre className="af-preview">
        {shown.map((line, i) => (
          <span className={diffLineClass(line)} key={i}>
            {line}
            {'\n'}
          </span>
        ))}
      </pre>
      {canCollapse && (
        <button
          type="button"
          className="af-preview-toggle"
          aria-expanded={previewOpen}
          onClick={() => setPreviewOpen((o) => !o)}
        >
          {previewOpen ? '收起预览' : `展开全部 ${approval.preview.length} 行`}
        </button>
      )}

      {!session && <p className="af-note warn">登录后可审批。</p>}
      {!real && session && (
        <p className="af-note">
          当前没有真实的待处理审批 —— 以上为示例运行的审批卡；操作将调用真实端点，失败会如实报错，不会伪装成功。
        </p>
      )}

      {!confirming ? (
        <div className="af-ctas">
          {!session ? (
            <button type="button" className="btn primary" onClick={openSignIn}>
              先登录
            </button>
          ) : (
            <button
              type="button"
              className="btn primary"
              disabled={disabled}
              onClick={() => runDecision('approve')}
            >
              {decide.isPending ? '提交中…' : '批准并恢复'}
            </button>
          )}
          <button
            type="button"
            className="btn sm"
            disabled={!session || decide.isPending}
            onClick={() => {
              reset()
              setInfo('重放端点尚未接入 —— 当前为示例运行。')
            }}
          >
            重放
          </button>
          <span className="spacer" />
          <button
            type="button"
            className="btn sm danger"
            disabled={!session || decide.isPending}
            onClick={() => {
              reset()
              setConfirming(true)
            }}
          >
            中止
          </button>
        </div>
      ) : (
        <div className="af-confirm" role="alert">
          <span>确定中止本次运行？已产生的检查点会保留。</span>
          <span className="spacer" />
          <button
            type="button"
            className="btn sm danger"
            onClick={() => {
              setConfirming(false)
              // No stop endpoint exists yet — report honestly instead of faking it.
              setInfo('中止端点尚未接入 —— 当前为示例运行，未真正中止。')
            }}
          >
            确认中止
          </button>
          <button type="button" className="btn sm" onClick={() => setConfirming(false)}>
            取消
          </button>
        </div>
      )}

      {info && (
        <p className="af-note" role="status">
          {info}
        </p>
      )}
      {error && (
        <p className="af-error" role="alert" title={errorDetail}>
          {error}
        </p>
      )}
    </section>
  )
}
