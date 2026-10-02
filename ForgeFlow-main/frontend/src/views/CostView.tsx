/**
 * CostView — 成本与预算 (INC2-25).
 *
 * Rewired onto the frozen cost contract:
 *   GET /cost/board   → budget board (tenant/team/task scope) + spend level
 *   GET /cost/savings → saved amount vs. a baseline (may have no data)
 *
 * The previous version read `/metrics/cost*` (four endpoints that all
 * `Depends(get_pool)` and therefore failed under the memory profile) and
 * rendered a pile of invented sample KPIs. This version shows ONLY real,
 * session/tenant-scoped figures: when `has_data=false` or a figure is null we
 * render 「—」 / 「不限」 rather than a misleading 0 or fabricated number.
 * Currency symbol is ¥ (CNY).
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  useCostBoard,
  useCostSavings,
  useDeleteBudget,
  useUpsertBudget,
} from '../api/hooks'
import type { CostBudgetRow, CostLevel } from '../api/client'
import { humanizeError } from '../api/errors'
import { ConfirmDialog } from '../components/ConfirmDialog'
import '../styles/cost.css'

const SCOPE_LABEL: Record<CostBudgetRow['scope'], string> = {
  tenant: '租户',
  team: '团队',
  task: '任务',
}

const LEVEL_LABEL: Record<CostLevel, string> = {
  ok: '正常',
  warn: '预警',
  exceeded: '超支',
}

function levelBadgeClass(level: CostLevel): string {
  if (level === 'exceeded') return 'badge red'
  if (level === 'warn') return 'badge amber'
  return 'badge emerald'
}

function currencySymbol(code: string): string {
  if (code === 'CNY') return '¥'
  if (code === 'USD') return '$'
  return ''
}

function money(v: number | null, code: string): string {
  if (v == null) return '—'
  return `${currencySymbol(code)}${v.toFixed(2)}`
}

export function CostView() {
  return (
    <section className="view active" data-screen-label="成本">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>成本与预算</h1>
            <p className="sub">实时成本看板 · 数据来自成本看板接口与节省核算接口</p>
          </div>
          <div className="actions">
            <a href="/api/cost/board" target="_blank" rel="noopener noreferrer" className="btn sm">
              看板 JSON →
            </a>
            <a href="/api/cost/savings" target="_blank" rel="noopener noreferrer" className="btn sm">
              节省 JSON →
            </a>
          </div>
        </div>
      </div>
      <div className="page-body">
        <KpiStrip />
        <div className="grid-2" style={{ marginTop: 16 }}>
          <BudgetPanel />
          <SavingsPanel />
        </div>
      </div>
    </section>
  )
}

function KpiStrip() {
  const board = useCostBoard()
  const savings = useCostSavings()
  const b = board.data
  const s = savings.data
  const code = b?.currency ?? s?.currency ?? 'CNY'
  const loading = board.isLoading || savings.isLoading

  return (
    <div className="kpi-strip">
      <div className="kpi">
        <span className="label">预算上限</span>
        <span className="val">
          {loading ? '…' : b?.total_limit == null ? '不限' : money(b.total_limit, code)}
        </span>
        <span className="delta">{b ? `币种 ${code}` : '—'}</span>
      </div>
      <div className="kpi">
        <span className="label">本期花费</span>
        <span className="val">{loading ? '…' : b ? money(b.total_spent, code) : '—'}</span>
        <span className="delta">{b ? '租户范围累计' : '—'}</span>
      </div>
      <div className="kpi">
        <span className="label">预算使用率</span>
        <span className="val">
          {loading ? '…' : b?.total_pct == null ? '—' : `${b.total_pct.toFixed(1)}%`}
        </span>
        <span className="delta">{b ? '相对预算上限' : '—'}</span>
      </div>
      <div className="kpi">
        <span className="label">预算级别</span>
        <span className="val" style={{ fontSize: 20 }}>
          {loading ? (
            '…'
          ) : b ? (
            <span className={levelBadgeClass(b.level)}>{LEVEL_LABEL[b.level]}</span>
          ) : (
            '—'
          )}
        </span>
        <span className="delta">80% 预警 · 100% 超支</span>
      </div>
      <div className="kpi">
        <span className="label">节省成本</span>
        <span className="val">{loading ? '…' : s && s.has_data ? money(s.amount, code) : '—'}</span>
        <span className="delta">{s && s.has_data ? '相对基线（本期）' : '暂无节省数据'}</span>
      </div>
    </div>
  )
}

function BudgetPanel() {
  const q = useCostBoard()
  const b = q.data
  const code = b?.currency ?? 'CNY'
  const budgets = b?.budgets ?? []
  // INC40 — 每行的待删预算（null ⇒ 未开确认框）。
  const [pendingDelete, setPendingDelete] = useState<CostBudgetRow | null>(null)
  const del = useDeleteBudget()

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">预算看板</div>
        <div className="actions">
          <span>{b ? `${budgets.length} 项预算` : '预算概览'}</span>
        </div>
      </div>
      {/* INC40 — 建/更预算（manager/admin 经 write:metrics 可达）。 */}
      <BudgetForm />
      <div className="panel-body flush">
        {q.isLoading ? (
          <p style={{ color: 'var(--fg-muted)', padding: 16 }}>加载中…</p>
        ) : q.isError ? (
          <p style={{ color: 'var(--danger-fg)', padding: 16 }} role="alert">
            加载失败：{(q.error as Error)?.message ?? '未知错误'}
          </p>
        ) : !b || !b.has_data || budgets.length === 0 ? (
          <p style={{ color: 'var(--fg-muted)', padding: 16 }}>
            暂无预算数据——发起任务并产生计费调用后自动汇总。
          </p>
        ) : (
          <div className="budget-list">
            {budgets.map((row, i) => (
              <BudgetItem
                key={`${row.scope}-${row.scope_id ?? 'all'}-${i}`}
                row={row}
                code={code}
                onDelete={() => setPendingDelete(row)}
              />
            ))}
          </div>
        )}
        {del.error && (
          <p style={{ color: 'var(--danger-fg)', padding: '8px 16px' }} role="alert">
            {humanizeError(del.error, '删除失败').label}
          </p>
        )}
      </div>

      {/* 二次确认（破坏性操作）：仅确认后才发出真实 `DELETE /cost/budgets/{scope}`。 */}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除该预算？"
        body="删除后该层级预算不再生效，看板将不再显示该行。"
        confirmLabel="删除"
        cancelLabel="取消"
        danger
        busy={del.isPending}
        onConfirm={() => {
          const target = pendingDelete
          if (!target || del.isPending) return
          del.mutate(
            { scope: target.scope, scopeId: target.scope_id },
            { onSettled: () => setPendingDelete(null) },
          )
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}

/** INC40 — 建/更新一条预算（`POST /cost/budgets`，upsert）。 */
function BudgetForm() {
  const [scope, setScope] = useState<'tenant' | 'team' | 'task'>('tenant')
  const [scopeId, setScopeId] = useState('')
  const [limit, setLimit] = useState('')
  const upsert = useUpsertBudget()

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const amount = Number(limit)
    if (!Number.isFinite(amount) || amount < 0 || upsert.isPending) return
    if (scope !== 'tenant' && !scopeId.trim()) return
    upsert.mutate(
      {
        scope,
        scope_id: scope === 'tenant' ? null : scopeId.trim(),
        limit_amount: amount,
      },
      {
        onSuccess: () => {
          setScopeId('')
          setLimit('')
        },
      },
    )
  }

  return (
    <form className="hub-toolbar budget-form" data-testid="budget-form" onSubmit={submit}>
      <select
        className="hub-select"
        data-testid="budget-scope"
        value={scope}
        aria-label="预算层级"
        onChange={(e) => setScope(e.target.value as 'tenant' | 'team' | 'task')}
      >
        <option value="tenant">租户</option>
        <option value="team">团队</option>
        <option value="task">任务</option>
      </select>
      <input
        className="hub-input"
        data-testid="budget-scope-id"
        value={scopeId}
        placeholder="团队 / 任务 ID（租户级留空）"
        aria-label="层级标识"
        autoComplete="off"
        disabled={scope === 'tenant'}
        onChange={(e) => setScopeId(e.target.value)}
      />
      <input
        className="hub-input"
        data-testid="budget-limit"
        value={limit}
        placeholder="预算上限金额"
        aria-label="预算上限金额"
        inputMode="decimal"
        autoComplete="off"
        onChange={(e) => setLimit(e.target.value)}
      />
      <button
        type="submit"
        className="btn sm primary"
        data-testid="budget-submit"
        disabled={upsert.isPending || !limit.trim() || (scope !== 'tenant' && !scopeId.trim())}
      >
        {upsert.isPending ? '保存中…' : '保存预算'}
      </button>
      {upsert.error && (
        <span className="af-note warn" role="alert" title={humanizeError(upsert.error, '保存失败').detail}>
          {humanizeError(upsert.error, '保存失败').label}
        </span>
      )}
    </form>
  )
}

function BudgetItem({
  row,
  code,
  onDelete,
}: {
  row: CostBudgetRow
  code: string
  onDelete: () => void
}) {
  const pct = row.pct
  const width = pct == null ? '0%' : `${Math.min(100, Math.max(0, pct))}%`
  return (
    <div className="budget-row">
      <div className="b-scope">
        <span className="name">{SCOPE_LABEL[row.scope] ?? row.scope}</span>
        <span className="id" title={row.scope_id ?? '全部'}>
          {row.scope_id ?? '全部'}
        </span>
      </div>
      <div>
        <div className="b-figures" style={{ marginBottom: 6 }}>
          <span className="spent">{money(row.spent, code)}</span>
          <span className="limit">{row.limit == null ? '不限' : `上限 ${money(row.limit, code)}`}</span>
        </div>
        <div className="budget-bar">
          <div className={`fill ${row.level}`} style={{ width }} />
        </div>
      </div>
      <div className="b-pct">
        {pct == null ? '—' : `${pct.toFixed(1)}%`}
        <span className={levelBadgeClass(row.level)} style={{ marginLeft: 8 }}>
          {LEVEL_LABEL[row.level]}
        </span>
        <button
          type="button"
          className="btn ghost sm"
          data-testid="budget-delete"
          style={{ marginLeft: 8 }}
          onClick={onDelete}
        >
          删除
        </button>
      </div>
    </div>
  )
}

function SavingsPanel() {
  const q = useCostSavings()
  const s = q.data
  const code = s?.currency ?? 'CNY'
  const hasData = !!s?.has_data && s?.amount != null
  const pctSaved =
    s && s.baseline != null && s.actual != null && s.baseline > 0
      ? (1 - s.actual / s.baseline) * 100
      : null

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">节省成本</div>
        <div className="actions">
          <span>{s ? `基线对比 · ${s.period}` : '节省对比'}</span>
        </div>
      </div>
      <div className="panel-body">
        {q.isLoading ? (
          <p style={{ color: 'var(--fg-muted)' }}>加载中…</p>
        ) : q.isError ? (
          <p style={{ color: 'var(--danger-fg)' }} role="alert">
            加载失败：{(q.error as Error)?.message ?? '未知错误'}
          </p>
        ) : !hasData ? (
          <p style={{ color: 'var(--fg-muted)', margin: 0 }}>
            暂无节省数据——需要基线窗口与本期实际成本才能计算节省金额。此处不会展示 0 或占位假值。
          </p>
        ) : (
          <>
            <div style={{ fontSize: 34, fontWeight: 500, letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums' }}>
              {money(s!.amount, code)}
            </div>
            <div className="b-figures" style={{ marginTop: 12, fontSize: 13 }}>
              <span className="limit">基线 {money(s!.baseline, code)}</span>
              <span className="spent">实际 {money(s!.actual, code)}</span>
            </div>
            <div style={{ marginTop: 14, fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--fg-muted)' }}>
              {pctSaved != null ? `节省约 ${pctSaved.toFixed(1)}%` : `倍数 ×${s!.multiplier.toFixed(2)}`}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
