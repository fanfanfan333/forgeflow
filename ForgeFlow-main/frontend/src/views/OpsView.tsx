/**
 * OpsView — 运维监控 / SLO attainment (INC2-25).
 *
 * Backed by GET /metrics/slo, which returns the three frozen tiers:
 *
 *   critical   99.5% availability / p95 1500 ms   (任务提交 · SSE · 策略 · 租户 · 审计)
 *   important  99.0% availability / p95 2000 ms   (记忆检索 · 技能安装 · 评测)
 *   edge       97.0% availability / p95 5000 ms   (分析 · 建议 · 演进 · 多模态)
 *
 * Honesty rule: the backend reports `has_data=false` for a tier with no
 * observations (never a fabricated 0% / perfect score). When that flag is
 * false we render 「无数据」 rather than 0%, and we never claim a breach we
 * cannot prove.
 */

import { useSloSummary } from '../api/hooks'
import type { SloTier } from '../api/client'
import '../styles/ops.css'

const TIER_LABEL: Record<string, string> = {
  critical: '关键链路',
  important: '重要能力',
  edge: '边缘能力',
}

const TIER_CAPS: Record<string, string> = {
  critical: '任务提交 · SSE · 策略 · 租户 · 审计',
  important: '记忆检索 · 技能安装 · 评测',
  edge: '分析 · 建议 · 演进 · 多模态',
}

const DEGRADE_LABEL: Record<string, string> = {
  none: '自动降级：无',
  keyword_search: '自动降级：关键词检索',
  disable_noncritical: '自动降级：暂停非关键能力',
}

function tierName(tier: string): string {
  return TIER_LABEL[tier] ?? tier
}

function fmtPct(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(2)}%`
}

function fmtMs(v: number | null): string {
  return v == null ? '—' : `${Math.round(v)} ms`
}

function tierBadge(t: SloTier) {
  if (!t.has_data) return <span className="badge">无数据</span>
  if (t.breaching) return <span className="badge red">● 已越界</span>
  return <span className="badge emerald">● 达标</span>
}

/** Meter fill: observed/target, capped at 100%, graded by breach state. */
function meterClass(t: SloTier): string {
  if (t.breaching) return 'fill fail'
  if (t.attainment != null && t.attainment < 1) return 'fill warn'
  return 'fill'
}

function meterWidth(t: SloTier): string {
  if (t.attainment == null) return '0%'
  return `${Math.min(100, Math.max(0, t.attainment * 100))}%`
}

export function OpsView() {
  const q = useSloSummary()
  const tiers = q.data?.tiers ?? []

  return (
    <section className="view active" data-screen-label="运维">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>运维监控 · SLO</h1>
            <p className="sub">
              三级服务目标达标率 · 数据来自服务等级目标接口
              {' · '}
              {q.isLoading ? '加载中…' : `共 ${tiers.length} 个层级`}
            </p>
          </div>
          <div className="actions">
            <a
              href="/api/metrics/slo"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm"
            >
              JSON →
            </a>
          </div>
        </div>
      </div>

      <div className="page-body">
        {q.isError ? (
          <div className="panel">
            <div className="panel-body">
              <p style={{ color: 'var(--red-4)', margin: 0 }} role="alert">
                SLO 数据加载失败：{(q.error as Error)?.message ?? '未知错误'}
              </p>
            </div>
          </div>
        ) : q.isLoading ? (
          <div className="grid-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="card slo-tier">
                <div className="skel" style={{ height: 120 }} />
              </div>
            ))}
          </div>
        ) : tiers.length === 0 ? (
          <div className="card empty">
            <span className="big">◔</span>
            暂无 SLO 层级数据
          </div>
        ) : (
          <>
            <div className="grid-3">
              {tiers.map((t) => (
                <TierCard key={t.tier} tier={t} />
              ))}
            </div>
            <div className="slo-note">
              观测值缺失时展示「无数据」而非 0%——后端对空窗口明确返回无数据标记，
              平台不会从空窗口推断达标或越界。p95 由窗口均值近似，真实分位数待直方图数据源接入。
            </div>
          </>
        )}
      </div>
    </section>
  )
}

function TierCard({ tier }: { tier: SloTier }) {
  return (
    <div className="card slo-tier">
      <div className="st-head">
        <div>
          <div className="st-name">{tierName(tier.tier)}</div>
          <div className="st-tier">{tier.tier}</div>
        </div>
        {tierBadge(tier)}
      </div>

      <div className="st-attain">
        {tier.has_data && tier.attainment != null ? (
          <>
            {(tier.attainment * 100).toFixed(1)}
            <span className="st-unit">%</span>
          </>
        ) : (
          <span className="st-none">无数据</span>
        )}
      </div>

      <div className="slo-meter" aria-hidden="true">
        <div className={meterClass(tier)} style={{ width: meterWidth(tier) }} />
      </div>

      <div className="st-caps">{TIER_CAPS[tier.tier] ?? '—'}</div>

      <div className="st-metrics">
        <div className="st-metric">
          <span className="lbl">可用性</span>
          <span className="val">
            目标 <b>{fmtPct(tier.availability_target)}</b>
            {' · '}
            实测 <b>{tier.has_data ? fmtPct(tier.availability_actual) : '无数据'}</b>
          </span>
        </div>
        <div className="st-metric">
          <span className="lbl">p95 延迟</span>
          <span className="val">
            目标 <b>{fmtMs(tier.p95_target_ms)}</b>
            {' · '}
            实测 <b>{tier.has_data ? fmtMs(tier.p95_actual_ms) : '无数据'}</b>
          </span>
        </div>
      </div>

      <div className="st-degrade">{DEGRADE_LABEL[tier.degrade] ?? `自动降级：${tier.degrade}`}</div>
    </div>
  )
}
