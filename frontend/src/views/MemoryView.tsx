import { useMemo, useState } from 'react'
import { useMemorySearch } from '../api/hooks'
import { ErrorText } from '../components/ErrorText'

export function MemoryView() {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const results = useMemorySearch(submitted)

  return (
    <section className="view active" data-screen-label="记忆">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>语义记忆</h1>
            <p className="sub">pgvector · ivfflat · 余弦相似度 · 命名空间隔离 · 检索实时来自记忆检索接口</p>
          </div>
          <div className="actions">
            <button className="btn sm" disabled title="命名空间筛选：后端检索接口已支持，选择器界面待接入">
              命名空间：销售/* ▾
            </button>
            <button className="btn sm" disabled title="嵌入模型由服务端配置">
              嵌入模型：text-embed-3-large ▾
            </button>
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/forgeflow/api/routers/memory.py"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm primary"
            >
              + 存储（文档）→
            </a>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="grid-2">
          <div>
            <SearchPanel
              query={query}
              setQuery={setQuery}
              onSubmit={() => setSubmitted(query)}
              results={results.data ?? []}
              isLoading={results.isLoading}
              isError={results.isError}
              error={results.error}
              submitted={submitted}
            />
          </div>
          <div>
            <EmbeddingScatter />
            <RecallHeatmap />
          </div>
        </div>
      </div>
    </section>
  )
}

type SearchPanelProps = {
  query: string
  setQuery: (q: string) => void
  onSubmit: () => void
  results: { id: string; content: string; similarity: number; namespace: string }[]
  isLoading: boolean
  isError: boolean
  error?: unknown
  submitted: string
}

function SearchPanel({ query, setQuery, onSubmit, results, isLoading, isError, error, submitted }: SearchPanelProps) {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">检索 · 余弦相似度</div>
        <div className="actions">
          <span>返回前 8 条</span>
        </div>
      </div>
      <div className="panel-body">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            onSubmit()
          }}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '10px 12px',
            border: '1px solid var(--border-default)',
            borderRadius: 6,
            background: 'var(--bg-page)',
          }}
        >
          <span style={{ color: 'var(--fg-muted)' }}>⌕</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入查询并按回车…"
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--fg-primary)',
              fontFamily: 'var(--font-sans)',
              fontSize: 13,
            }}
          />
          <span className="kbd">⏎</span>
        </form>

        <div style={{ marginTop: 14, display: 'grid', gap: 8 }}>
          {!submitted && (
            <p style={{ margin: '0 0 2px', fontSize: 11.5, color: 'var(--fg-muted)' }}>
              <span className="badge amber" style={{ fontSize: 10, marginRight: 6 }}>示例</span>
              以下为示例结果 —— 输入查询并按回车即可实时检索你的记忆。
            </p>
          )}
          {!submitted && <SampleResults />}
          {isLoading && (
            <p style={{ color: 'var(--fg-muted)', textAlign: 'center', padding: 24 }}>加载中…</p>
          )}
          {isError && (
            <p style={{ color: 'var(--danger-fg)', padding: 16 }}>
              <ErrorText error={error} label="检索失败" />
            </p>
          )}
          {submitted && !isLoading && results.length === 0 && !isError && (
            <p style={{ color: 'var(--fg-muted)', textAlign: 'center', padding: 24 }}>
              未匹配到任何记忆。
            </p>
          )}
          {results.map((r) => (
            <div className="mem-card" key={r.id}>
              <div className="top">
                <span className="ns">{r.namespace}</span>
                <span className="sim">相似度 {r.similarity.toFixed(2)}</span>
              </div>
              <div className="snippet">{r.content}</div>
              <div className="footer">
                <span className="badge mono">{r.id.slice(0, 8)}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// Sample results shown before the user has typed anything — matches design.
function SampleResults() {
  return (
    <>
      <div className="mem-card">
        <div className="top">
          <span className="ns">命名空间：销售/Stripe · 决策记录</span>
          <span className="sim">相似度 0.89</span>
        </div>
        <div className="snippet">Stripe 拒绝了 2025 年的扩展合作，理由是现有的支付服务商合同延续至 2026 年第二季度。续约窗口后再重新接触。</div>
        <div className="footer">
          <span className="badge">2025-11-20</span>
          <span className="badge">林薇</span>
          <span className="badge mono">Token 数 3</span>
        </div>
      </div>
      <div className="mem-card">
        <div className="top">
          <span className="ns">命名空间：销售/Stripe · 互动记录</span>
          <span className="sim">相似度 0.84</span>
        </div>
        <div className="snippet">工程副总裁在高管晚宴上体验了 ForgeFlow，对收入运营自动化表现出兴趣。负责人：陈思。</div>
        <div className="footer">
          <span className="badge">2026-02-08</span>
          <span className="badge">陈思</span>
          <span className="badge mono">Token 数 12</span>
        </div>
      </div>
      <div className="mem-card">
        <div className="top">
          <span className="ns">命名空间：策略/全局 · 策略</span>
          <span className="sim">相似度 0.78</span>
        </div>
        <div className="snippet">新增年度经常性收入 ≥ ¥100K 在发送前需副总裁级审批。适用于所有销售线索资质评估工作流。</div>
        <div className="footer">
          <span className="badge">策略</span>
          <span className="badge">张凯</span>
          <span className="badge mono">全局</span>
        </div>
      </div>
      <div className="mem-card">
        <div className="top">
          <span className="ns">命名空间：销售/广达 · 决策记录</span>
          <span className="sim">相似度 0.62</span>
        </div>
        <div className="snippet">广达在 2026 年第一季度完成了 ¥84K 的扩展合作；关键推动人 = 平台负责人。可作为标杆客户。</div>
        <div className="footer">
          <span className="badge">2026-03-14</span>
          <span className="badge">金杰</span>
          <span className="badge mono">Token 数 8</span>
        </div>
      </div>
    </>
  )
}

// Deterministic point cloud (replaces design's document.write random scatter).
function makeScatter(seed: number) {
  let s = seed >>> 0
  function rnd() {
    s |= 0; s = (s + 0x6D2B79F5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  const clusters = [
    { x: 140, y: 120, r: 55, color: 'var(--blue-4)', n: 80 },
    { x: 320, y: 160, r: 48, color: 'var(--purple-4)', n: 60 },
    { x: 200, y: 270, r: 60, color: 'var(--emerald-4)', n: 90 },
    { x: 370, y: 290, r: 42, color: 'var(--amber-4)', n: 50 },
  ]
  const out: { x: number; y: number; color: string }[] = []
  for (const cl of clusters) {
    for (let i = 0; i < cl.n; i++) {
      const a = rnd() * Math.PI * 2
      const r = Math.sqrt(rnd()) * cl.r
      out.push({ x: cl.x + Math.cos(a) * r, y: cl.y + Math.sin(a) * r, color: cl.color })
    }
  }
  return out
}

function EmbeddingScatter() {
  const points = useMemo(() => makeScatter(7), [])
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">嵌入空间 · 二维投影</div>
        <div className="actions">
          <span className="badge amber" style={{ fontSize: 10 }}>示例</span>
          <span>降维投影</span>
        </div>
      </div>
      <div className="panel-body">
        <svg viewBox="0 0 480 380" width="100%" height={380} role="img" aria-label="嵌入空间二维投影示例，展示四个命名空间簇（销售/Stripe、销售/云枢、策略/全局、客服）以及当前查询的标记点。">
          <rect width="480" height="380" fill="var(--bg-inset)" rx="6" />
          <g opacity="0.16">
            <ellipse cx="140" cy="120" rx="80" ry="60" fill="var(--blue-4)" />
            <ellipse cx="320" cy="160" rx="70" ry="50" fill="var(--purple-4)" />
            <ellipse cx="200" cy="270" rx="90" ry="55" fill="var(--emerald-4)" />
            <ellipse cx="370" cy="290" rx="60" ry="50" fill="var(--amber-4)" />
          </g>
          <g>
            {points.map((p, i) => (
              <circle key={i} cx={p.x.toFixed(1)} cy={p.y.toFixed(1)} r="1.6" fill={p.color} opacity="0.7" />
            ))}
          </g>
          <circle cx="178" cy="148" r="6" fill="none" stroke="var(--fg-primary)" strokeWidth="1.5" />
          <circle cx="178" cy="148" r="3" fill="var(--fg-primary)" />
          <text x="190" y="146" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-primary)">
            你的查询
          </text>
          <g fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)">
            <text x="100" y="56">销售/Stripe</text>
            <text x="296" y="100">销售/云枢</text>
            <text x="160" y="338">策略/全局</text>
            <text x="334" y="346">客服/*</text>
          </g>
        </svg>
      </div>
    </div>
  )
}

// Deterministic heatmap intensities — same PRNG so the page is stable across renders.
function makeHeatmap(seed: number, rows: number, cols: number) {
  let s = seed >>> 0
  function rnd() {
    s |= 0; s = (s + 0x6D2B79F5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  return Array.from({ length: rows }, () => Array.from({ length: cols }, () => 0.1 + rnd() * 0.9))
}

const NAMESPACES = ['销售/*', '客服/*', '财务/*', '策略/全局', '智能体/*']

function RecallHeatmap() {
  const data = useMemo(() => makeHeatmap(11, NAMESPACES.length, 24), [])
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <div className="title">召回热力图 · 最近 24 小时</div>
        <div className="actions">
          <span className="badge amber" style={{ fontSize: 10 }}>示例</span>
          <span>按命名空间 × 小时</span>
        </div>
      </div>
      <div className="panel-body">
        <div style={{ display: 'grid', gridTemplateColumns: '80px repeat(24, 1fr)', gap: 2, fontFamily: 'var(--font-mono)', fontSize: 9 }}>
          {NAMESPACES.map((ns, r) => (
            <div key={ns} style={{ display: 'contents' }}>
              <div style={{ color: 'var(--fg-muted)', padding: '2px 0' }}>{ns}</div>
              {data[r].map((v, c) => (
                <div
                  key={c}
                  style={{
                    aspectRatio: '1',
                    background: 'var(--blue-4)',
                    opacity: v.toFixed(2),
                    borderRadius: 1,
                  }}
                  title={`${ns} · h${c}`}
                />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
