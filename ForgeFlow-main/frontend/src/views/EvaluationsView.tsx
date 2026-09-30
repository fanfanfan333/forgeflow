import { useEvaluationSummary } from '../api/hooks'

export function EvaluationsView() {
  const q = useEvaluationSummary()
  const e = q.data

  const overall = e
    ? ((e.avg_faithfulness + e.avg_relevance + e.avg_coherence) / 3).toFixed(1)
    : '—'

  return (
    <section className="view active" data-screen-label="评测">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>评测</h1>
            <p className="sub">
              LLM 评审 · 忠实度 · 相关性 · 连贯性 ·{' '}
              {e ? `采样 ${e.sample_count} 次运行` : '—'}
            </p>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="kpi-strip">
          <div className="kpi">
            <span className="label">综合得分</span>
            <span className="val" style={{ color: 'var(--emerald-4)' }}>
              {overall}<span className="u">/10</span>
            </span>
            <span className="delta">综合</span>
          </div>
          <div className="kpi">
            <span className="label">忠实度</span>
            <span className="val">{e ? e.avg_faithfulness.toFixed(1) : '—'}</span>
            <span className="delta">事实依据</span>
          </div>
          <div className="kpi">
            <span className="label">相关性</span>
            <span className="val">{e ? e.avg_relevance.toFixed(1) : '—'}</span>
            <span className="delta">切题程度</span>
          </div>
          <div className="kpi">
            <span className="label">连贯性</span>
            <span className="val">{e ? e.avg_coherence.toFixed(1) : '—'}</span>
            <span className="delta">可读性</span>
          </div>
          <div className="kpi">
            <span className="label">幻觉率</span>
            <span className="val" style={{ color: (e?.hallucination_rate ?? 0) > 0.01 ? 'var(--red-4)' : 'var(--emerald-4)' }}>
              {e ? (e.hallucination_rate * 100).toFixed(2) : '—'}<span className="u">%</span>
            </span>
            <span className="delta">无依据论断</span>
          </div>
        </div>

        <div className="grid-2" style={{ marginTop: 16 }}>
          <div className="panel">
            <div className="panel-head">
              <div className="title">得分分布 · 示例</div>
            </div>
            <div className="panel-body">
              <svg viewBox="0 0 480 200" width="100%" height={200} role="img" aria-label="评审得分直方图示例，分数区间 4.0 到 10，峰值约在 8.5。">
                {[2, 3, 5, 8, 14, 22, 38, 68, 110, 142, 118, 68].map((v, i, arr) => {
                  const max = Math.max(...arr)
                  const h = (v / max) * 160
                  const x = 20 + i * 38
                  return (
                    <g key={i}>
                      <rect x={x} y={180 - h} width={30} height={h} fill="var(--blue-4)" opacity={0.4 + (i / arr.length) * 0.6} rx={2} />
                      <text x={x + 15} y={195} textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">
                        {(i * 0.5 + 4).toFixed(1)}
                      </text>
                    </g>
                  )
                })}
              </svg>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <div className="title">失败类型 · 示例</div>
            </div>
            <div className="panel-body">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <FailBar label="幻觉 · 无依据论断" pct={38} count={42} color="var(--red-3)" />
                <FailBar label="工具失败 · 超时" pct={25} count={28} color="var(--amber-3)" />
                <FailBar label="策略拦截 · 敏感信息" pct={20} count={22} color="var(--purple-3)" />
                <FailBar label="预算护栏中止" pct={11} count={12} color="var(--blue-3)" />
                <FailBar label="结构不匹配" pct={6} count={6} color="var(--emerald-3)" />
              </div>
              <ForgeRootCause />
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function ForgeRootCause() {
  return (
    <div
      style={{
        marginTop: 18,
        padding: 12,
        background: 'var(--bg-inset)',
        borderRadius: 6,
        borderLeft: '2px solid var(--blue-4)',
        fontSize: 12.5,
        color: 'var(--fg-secondary)',
      }}
    >
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--blue-4)', letterSpacing: '.12em', textTransform: 'uppercase' }}>
        FORGE · 根因分析 · 预览
      </span>
      <br />
      <span style={{ color: 'var(--fg-primary)' }}>42</span> 次幻觉集中在{' '}
      <span style={{ color: 'var(--fg-primary)' }}>研究员</span> 抓取页面 &gt;9KB 的提示上。将内容截断到 6KB 可把
      幻觉率降到{' '}
      <span className="mono" style={{ color: 'var(--emerald-4)' }}>~0.03%</span>，且不增加评审成本。
      <div style={{ marginTop: 8 }}>
        <a
          href="https://github.com/JoelJohnsonThomas/forgeflow/discussions/categories/ideas"
          target="_blank"
          rel="noopener noreferrer"
          className="btn sm primary"
        >
          讨论此修复 →
        </a>
      </div>
    </div>
  )
}

function FailBar({ label, pct, count, color }: { label: string; pct: number; count: number; color: string }) {
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
        <span>{label}</span>
        <span className="mono">
          {count} · {pct}%
        </span>
      </div>
      <div style={{ height: 8, background: 'var(--bg-inset)', borderRadius: 4, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color }} />
      </div>
    </div>
  )
}
