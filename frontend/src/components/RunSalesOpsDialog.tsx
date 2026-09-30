import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../api/client'
import type { RunWorkflowResponse, SalesLeadInput } from '../api/client'
import { humanizeError } from '../api/errors'
import { useRunSalesOps } from '../api/hooks'
import { useSession } from '../hooks/useSession'
import { openSignIn } from './authEvents'
import { statusLabel } from '../i18n/labels'
import '../styles/auth.css'

const INDUSTRIES: NonNullable<SalesLeadInput['industry']>[] = [
  'saas',
  'fintech',
  'healthcare',
  'enterprise',
  'ecommerce',
  'martech',
  'other',
]

// Display labels only — the option `value` stays the English enum the API and
// the sales_ops graph expect.
const INDUSTRY_LABELS: Record<NonNullable<SalesLeadInput['industry']>, string> = {
  saas: 'SaaS',
  fintech: '金融科技',
  healthcare: '医疗健康',
  enterprise: '大型企业',
  ecommerce: '电子商务',
  martech: '营销科技',
  other: '其他',
}

/**
 * Trigger a real sales_ops run (POST /workflows/run). The graph executes
 * synchronously — researcher → analyzer → executor — so the request takes
 * 1–2 minutes; the dialog stays open with a progress note, then shows where
 * the run landed (pending approval vs. disqualified/completed).
 */
export function RunSalesOpsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const session = useSession()
  const run = useRunSalesOps()
  const [company, setCompany] = useState('')
  const [contactName, setContactName] = useState('')
  const [contactEmail, setContactEmail] = useState('')
  const [industry, setIndustry] = useState('')
  const [budget, setBudget] = useState('')
  const [context, setContext] = useState('')
  const [result, setResult] = useState<RunWorkflowResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | undefined>(undefined)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    firstFieldRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !run.isPending) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose, run.isPending])

  if (!open) return null

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setErrorDetail(undefined)
    setResult(null)
    const lead: SalesLeadInput = { company_name: company.trim() }
    if (contactName.trim()) lead.contact_name = contactName.trim()
    if (contactEmail.trim()) lead.contact_email = contactEmail.trim()
    if (industry) lead.industry = industry as SalesLeadInput['industry']
    if (budget.trim()) lead.known_budget_usd = Math.max(0, Math.round(Number(budget)))
    if (context.trim()) lead.additional_context = context.trim().slice(0, 1000)
    try {
      setResult(await run.mutateAsync(lead))
    } catch (err) {
      const h = humanizeError(err, '运行失败')
      if (err instanceof ApiError && err.status === 401) {
        setError('会话已过期，请重新登录后再试。')
      } else if (err instanceof ApiError && err.status === 403) {
        setError('当前角色无法触发工作流（viewer 为只读）。请以 rep-1 或 manager-1 身份登录。')
      } else if (err instanceof ApiError && err.status === 422) {
        setError(humanizeError(err, '接口拒绝了该请求').label)
      } else if (err instanceof ApiError && err.status === 504) {
        setError('运行超时（某个 LLM 或工具调用卡住了）。请查看“实时运行”，该任务可能仍会完成。')
      } else {
        setError(h.label)
      }
      // Keep the raw backend text for the title= tooltip — never lose it.
      setErrorDetail(h.detail)
    }
  }

  const close = () => {
    if (run.isPending) return
    setResult(null)
    setError(null)
    setErrorDetail(undefined)
    onClose()
  }

  return (
    <div className="auth-overlay" onClick={close}>
      <div
        className="auth-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-dialog-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="run-dialog-title">运行「销售线索资质评估」工作流</h2>

        {!session ? (
          <>
            <p className="auth-hint">
              触发工作流会调用运行接口，需要先登录。
            </p>
            <div className="auth-actions">
              <button type="button" className="btn" onClick={close}>
                取消
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={() => {
                  close()
                  openSignIn()
                }}
              >
                先登录
              </button>
            </div>
          </>
        ) : result ? (
          <>
            <p className="auth-hint">
              运行 <code>{result.run_id.slice(0, 8)}</code> 已完成智能体流水线，状态为{' '}
              <code>{statusLabel(result.status)}</code>
              {result.message ? <> —— {result.message}</> : null}。
            </p>
            <p className="auth-hint">
              {result.status === 'pending_approval' ? (
                <>该提案正在等待经理在 <a href="/console/approvals">审批</a> 中处理。</>
              ) : (
                <>
                  可在 <a href="/console/runs">实时运行</a> 中查看。（评分低于 4.0 的线索会被判定为不合格，
                  无需审批即可完成。）
                </>
              )}
            </p>
            <div className="auth-actions">
              <button type="button" className="btn primary" onClick={close}>
                完成
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="auth-hint">
              运行真实流水线：研究员 → 分析器（为线索评分；&lt;4.0 判定不合格）→ 执行器
              起草提案 → 暂停等待经理审批。耗时约 <b>1–2 分钟</b>，会消耗真实的 LLM 词元。
            </p>
            <form onSubmit={submit}>
              <label>
                <span>公司名称（必填）</span>
                <input
                  ref={firstFieldRef}
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  required
                  maxLength={256}
                  disabled={run.isPending}
                />
              </label>
              <label>
                <span>联系人姓名</span>
                <input value={contactName} onChange={(e) => setContactName(e.target.value)} disabled={run.isPending} />
              </label>
              <label>
                <span>联系人邮箱</span>
                <input
                  type="email"
                  value={contactEmail}
                  onChange={(e) => setContactEmail(e.target.value)}
                  disabled={run.isPending}
                />
              </label>
              <label>
                <span>行业</span>
                <select value={industry} onChange={(e) => setIndustry(e.target.value)} disabled={run.isPending}>
                  <option value="">— 可选 —</option>
                  {INDUSTRIES.map((i) => (
                    <option key={i} value={i}>
                      {INDUSTRY_LABELS[i]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>已知预算（USD）</span>
                <input
                  type="number"
                  min={0}
                  step={1000}
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  disabled={run.isPending}
                />
              </label>
              <label>
                <span>补充信息（信息越完整，资质评估越准确）</span>
                <textarea
                  rows={3}
                  maxLength={1000}
                  value={context}
                  onChange={(e) => setContext(e.target.value)}
                  disabled={run.isPending}
                  placeholder="例如：D 轮融资、500+ 员工、Q3 预算已确认、对接人拥有签约权限…"
                />
              </label>
              {error && (
                <p className="auth-error" role="alert" title={errorDetail}>
                  {error}
                </p>
              )}
              {run.isPending && (
                <p className="auth-hint" role="status">
                  运行中——调度器正在编排各智能体（研究员 → 分析器 → 执行器）。通常需要 1–2 分钟，
                  请保持此窗口打开。
                </p>
              )}
              <div className="auth-actions">
                <button type="button" className="btn" onClick={close} disabled={run.isPending}>
                  取消
                </button>
                <button type="submit" className="btn primary" disabled={run.isPending || !company.trim()}>
                  {run.isPending ? '运行中…' : '运行工作流'}
                </button>
              </div>
            </form>
          </>
        )}
      </div>
    </div>
  )
}
