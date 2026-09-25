/**
 * HomeView — AgentFlow home page (PRD §7).
 *
 * All blocks render REAL API data (no hardcoded numbers):
 *   · Hero + task input + suggestion chips   ← role-aware (home/roleConfig.ts)
 *   · 4 KPI cards          ← useHomeKpis() (single source: /metrics + /cost/savings)
 *   · 我的 Agent (6)        ← /agents/catalog
 *   · 技能中心 (4)          ← /skills?featured=true
 *   · 近期任务              ← /runs
 *   · 智能执行日志          ← /runs/{id}/events (SSE)
 *   · 安全与资源概览        ← /security/overview
 *
 * KPI #3 「节省成本」 (PRD §7.2-D) is wired to the real /cost/savings contract:
 * it shows the saved `amount` when a baseline exists, and 「—」 otherwise —
 * never a fabricated 0 or a stand-in cost figure. All four KPI values come
 * from `useHomeKpis()` so the view is a pure renderer.
 */

import { useMemo, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import {
  useAgentCatalog,
  useCreateTask,
  useFeaturedSkills,
  useHomeKpis,
  useRecentHubRuns,
  useSecurityOverview,
} from '../api/hooks'
import type { HomeKpis } from '../api/hooks'
import { useRunEvents } from '../hooks/useRunEvents'
import { useSession } from '../hooks/useSession'
import { roleConfigFor } from '../home/roleConfig'
import type { KpiId } from '../home/roleConfig'
import type { RunEventPayload } from '../api/sse'
import type { PlatformAgent, Skill, RunSummary } from '../api/client'
import { IconBot, IconChart, IconCompass, IconDocument, IconSearch, IconShield, IconTerminal } from '../components/icons'
import '../styles/home.css'

// Monochrome single-stroke line icons (no colourful emoji) — the icon colour is
// pinned to --fg-muted so the glyph reads as a restrained outline even inside a
// tinted `.a-ico` chip.
const ICON_TINT = { color: 'var(--fg-muted)' } as const

const AGENT_ICON: Record<string, ReactNode> = {
  planner: <IconCompass width={18} height={18} style={ICON_TINT} />,
  data: <IconChart width={18} height={18} style={ICON_TINT} />,
  code: <IconTerminal width={18} height={18} style={ICON_TINT} />,
  research: <IconSearch width={18} height={18} style={ICON_TINT} />,
  document: <IconDocument width={18} height={18} style={ICON_TINT} />,
  security: <IconShield width={18} height={18} style={ICON_TINT} />,
}

const AGENT_ICON_FALLBACK = <IconBot width={18} height={18} style={ICON_TINT} />

const CUBES: { label: string; cls: string }[] = [
  { label: 'Agent', cls: 'c1' },
  { label: '技能', cls: 'c2' },
  { label: '记忆', cls: 'c3' },
  { label: '安全', cls: 'c4' },
]

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const diff = Math.max(0, Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

export function HomeView() {
  return (
    <section className="view active" data-screen-label="首页">
      <div className="home">
        <div className="home-main">
          <Hero />
          <KpiRow />
          <AgentSection />
          <SkillSection />
        </div>
        <aside className="home-rail">
          <RecentTasks />
          <ExecutionLog />
          <SecurityOverviewCard />
        </aside>
      </div>
    </section>
  )
}

/* ---- Hero ---------------------------------------------------------------- */

function Hero() {
  const [intent, setIntent] = useState('')
  const create = useCreateTask()
  const session = useSession()
  const role = roleConfigFor(session?.role)
  const busy = create.isPending
  // Read-only roles (viewer) lack `execute:workflows`, so the hero must not
  // offer a submit path that would 403 (see home/roleConfig.ts).
  const canExecute = role.canExecute
  const ready = canExecute && intent.trim().length > 0 && !busy

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!ready) return
    create.mutate(
      { intent: intent.trim() },
      { onSuccess: () => setIntent('') },
    )
  }

  return (
    <div className="hero">
      <div className="hero-art" aria-hidden="true">
        {CUBES.map((c) => (
          <div key={c.label} className={`cube ${c.cls}`}>
            {c.label}
          </div>
        ))}
      </div>
      <div className="hero-inner">
        <h1 className="hero-title">
          你好，欢迎使用 <span className="accent">企业级 Multi-Agent 智能工作与技能资产平台</span>
        </h1>
        <p className="hero-sub">
          基于 LangGraph + MCP，构建安全、可控、可持续进化的企业级 AI 员工体系
          <span className="text-muted"> · 当前身份：{role.label}</span>
        </p>
        <form className="hero-input" onSubmit={submit}>
          <input
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder={canExecute ? '告诉我你想完成什么任务…' : '当前身份为只读，无法发起任务'}
            aria-label="任务输入"
            disabled={!canExecute}
          />
          <button
            type="submit"
            className={`hero-send${ready ? ' ready' : ''}${busy ? ' busy' : ''}`}
            disabled={!ready}
            title={canExecute ? '提交任务' : '只读身份无法提交任务'}
            aria-label="提交任务"
          >
            {busy ? '…' : '➤'}
          </button>
        </form>
        <div className="hero-chips">
          {role.suggestions.map((s) =>
            s.to ? (
              <a key={s.label} className="chip" href={s.to}>
                {s.label}
              </a>
            ) : (
              <button
                key={s.label}
                type="button"
                className="chip"
                onClick={() => setIntent(s.label)}
              >
                {s.label}
              </button>
            ),
          )}
        </div>
        {!canExecute && (
          <p className="text-muted" style={{ marginTop: 12 }} role="note">
            当前身份为只读访客，无法发起任务
          </p>
        )}
        {create.isError && (
          <p className="kpi-card k-delta down" style={{ marginTop: 12, border: 0, padding: 0 }} role="alert">
            提交失败：{(create.error as Error)?.message ?? '未知错误'}
          </p>
        )}
      </div>
    </div>
  )
}

/* ---- KPI ----------------------------------------------------------------- */

const KPI_META: Record<KpiId, { label: string; icon: string; tone: string }> = {
  total_runs: { label: '总任务数', icon: '◎', tone: 'ico-blue' },
  success_rate: { label: '成功率', icon: '✓', tone: 'ico-emerald' },
  savings: { label: '节省成本', icon: '¥', tone: 'ico-purple' },
  avg_response: { label: '平均响应时间', icon: '◷', tone: 'ico-amber' },
}

type KpiView = {
  label: string
  icon: string
  tone: string
  value: ReactNode
  delta: { text: string; kind: 'up' | 'down' | 'neutral' }
}

function savingsSub(s: HomeKpis['savings']): string {
  if (!s.hasData || s.amount == null) return '暂无基线，无法估算节省'
  if (s.baseline != null && s.actual != null && s.baseline > 0) {
    const savedPct = (1 - s.actual / s.baseline) * 100
    return `相比基线节省 ${savedPct.toFixed(0)}%`
  }
  return `相比基线 ×${s.multiplier.toFixed(2)}`
}

function buildKpi(id: KpiId, k: HomeKpis): KpiView {
  const meta = KPI_META[id]
  switch (id) {
    case 'total_runs':
      return {
        ...meta,
        value: k.totalRuns.hasData ? k.totalRuns.value.toLocaleString() : '—',
        delta: { text: k.totalRuns.hasData ? '全部历史任务' : '暂无任务', kind: 'neutral' },
      }
    case 'success_rate':
      return {
        ...meta,
        value: k.successRate.hasData ? (
          <>
            {(k.successRate.value * 100).toFixed(1)}
            <span className="u">%</span>
          </>
        ) : (
          '—'
        ),
        delta: {
          text: k.successRate.hasData ? `基于 ${k.successRate.sampleSize} 次终态任务` : '暂无终态任务',
          kind: k.successRate.hasData ? 'up' : 'neutral',
        },
      }
    case 'savings':
      return {
        ...meta,
        value: k.savings.hasData && k.savings.amount != null ? `¥${k.savings.amount.toFixed(2)}` : '—',
        delta: { text: savingsSub(k.savings), kind: 'neutral' },
      }
    case 'avg_response':
      return {
        ...meta,
        value: k.avgLatencyMs.hasData ? (
          <>
            {(k.avgLatencyMs.value / 1000).toFixed(1)}
            <span className="u">s</span>
          </>
        ) : (
          '—'
        ),
        delta: {
          text: k.avgLatencyMs.hasData ? `近 ${k.avgLatencyMs.sampleSize} 次任务均值` : '暂无任务',
          kind: 'neutral',
        },
      }
  }
}

function KpiRow() {
  const kpis = useHomeKpis()
  const session = useSession()
  const role = roleConfigFor(session?.role)

  return (
    <div className="kpi-row">
      {role.kpiOrder.map((id) => (
        <KpiCard key={id} {...buildKpi(id, kpis)} loading={kpis.loading} />
      ))}
    </div>
  )
}

function KpiCard({
  label,
  icon,
  tone,
  value,
  delta,
  loading,
}: {
  label: string
  icon: string
  tone: string
  value: ReactNode
  delta: { text: string; kind: 'up' | 'down' | 'neutral' }
  loading: boolean
}) {
  return (
    <div className="card kpi-card">
      <div className="top">
        <span className="k-label">{label}</span>
        <span className={`k-ico ${tone}`} aria-hidden="true">{icon}</span>
      </div>
      {loading ? (
        <>
          <div className="skel skel-val" />
          <div className="skel skel-line" style={{ width: '50%' }} />
        </>
      ) : (
        <>
          <span className="k-val">{value}</span>
          <span className={`k-delta ${delta.kind}`}>{delta.text}</span>
        </>
      )}
    </div>
  )
}

/* ---- 我的 Agent ----------------------------------------------------------- */

function AgentSection() {
  const q = useAgentCatalog()
  const agents = q.data ?? []
  return (
    <div>
      <div className="sec-head">
        <div>
          <div className="sec-title">我的 Agent</div>
          <div className="sec-sub">智能协作的多智能体团队</div>
        </div>
        <a className="sec-link" href="/agents">查看全部 →</a>
      </div>
      {q.isLoading ? (
        <div className="agent-grid">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="card agent-card"><div className="skel" style={{ height: 90 }} /></div>
          ))}
        </div>
      ) : agents.length === 0 ? (
        <div className="card empty"><span className="big"><IconBot width={22} height={22} /></span>暂无 Agent</div>
      ) : (
        <div className="agent-grid">
          {agents.map((a) => (
            <AgentCard key={a.agent_id} agent={a} />
          ))}
        </div>
      )}
    </div>
  )
}

function AgentCard({ agent }: { agent: PlatformAgent }) {
  const tone = ['ico-blue', 'ico-purple', 'ico-emerald', 'ico-amber'][
    Math.abs(agent.agent_id.length) % 4
  ]
  const icon = AGENT_ICON[agent.agent_id] ?? AGENT_ICON_FALLBACK
  return (
    <a className="card agent-card" href="/agents" style={{ textDecoration: 'none' }}>
      <div className="a-top">
        <span className={`a-ico ${tone}`} aria-hidden="true">{icon}</span>
        <span className="a-status"><i className="dot live" />{agent.status}</span>
      </div>
      <span className="a-name">{agent.name}</span>
      <span className="a-cat">{agent.category}</span>
      <span className="a-desc">{agent.description}</span>
    </a>
  )
}

/* ---- 技能中心 ------------------------------------------------------------- */

function SkillSection() {
  const q = useFeaturedSkills(4)
  const skills = q.data?.items ?? []
  return (
    <div>
      <div className="sec-head">
        <div>
          <div className="sec-title">技能中心</div>
          <div className="sec-sub">沉淀团队经验，构建可复用的技能资产</div>
        </div>
        <a className="sec-link" href="/skills">查看全部 →</a>
      </div>
      {q.isLoading ? (
        <div className="skill-row">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card skill-card"><div className="skel" style={{ height: 60 }} /></div>
          ))}
        </div>
      ) : skills.length === 0 ? (
        <div className="card empty"><span className="big">✦</span>暂无技能</div>
      ) : (
        <div className="skill-row">
          {skills.map((s) => (
            <SkillCard key={s.id} skill={s} />
          ))}
        </div>
      )}
    </div>
  )
}

function SkillCard({ skill }: { skill: Skill }) {
  return (
    <a className="card skill-card" href="/skills">
      <span className="s-name">{skill.name}</span>
      <div className="s-foot">
        <span className="badge purple">{skill.domain}</span>
        <span className="s-uses">{skill.usage_count} 次</span>
      </div>
    </a>
  )
}

/* ---- 近期任务 ------------------------------------------------------------- */

function RecentTasks() {
  const q = useRecentHubRuns(6)
  const runs = q.data?.items ?? []
  return (
    <div className="card rail-card">
      <div className="rail-head">
        <span className="t">近期任务</span>
        <a className="sec-link" href="/tasks">查看全部 →</a>
      </div>
      <div className="rail-body">
        {q.isLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="task-row"><div className="skel" style={{ height: 30, width: '100%' }} /></div>
          ))
        ) : runs.length === 0 ? (
          <div className="empty">
            <span className="big">◔</span>
            还没有任务，去上方发起第一个任务吧
          </div>
        ) : (
          runs.map((r) => <TaskRow key={r.run_id} run={r} />)
        )}
      </div>
    </div>
  )
}

function TaskRow({ run }: { run: RunSummary }) {
  const done = run.status === 'completed'
  const tone = done ? 'ico-emerald' : run.status === 'failed' ? 'ico-amber' : 'ico-blue'
  return (
    <div className="task-row">
      <span className={`t-ico ${tone}`} aria-hidden="true">◈</span>
      <div className="t-main">
        <div className="t-title">{run.title || run.intent}</div>
        <div className="t-meta">{relativeTime(run.created_at)}</div>
      </div>
      {done ? (
        <span className="badge emerald">● 已完成</span>
      ) : run.status === 'failed' ? (
        <span className="badge red">● 失败</span>
      ) : (
        <span className="badge blue">● 进行中</span>
      )}
    </div>
  )
}

/* ---- 智能执行日志 --------------------------------------------------------- */

function ExecutionLog() {
  const runsQ = useRecentHubRuns(1)
  const latestId = runsQ.data?.items?.[0]?.run_id ?? null
  const { events, done, error } = useRunEvents(latestId)

  const lines = useMemo(() => events.map(toLogLine).slice(-12).reverse(), [events])

  return (
    <div className="card rail-card">
      <div className="rail-head">
        <span className="t">智能执行日志</span>
        <a className="sec-link" href="/tasks">查看全部 →</a>
      </div>
      <div className="rail-body">
        {!latestId ? (
          <div className="empty"><span className="big">≋</span>暂无执行记录</div>
        ) : lines.length === 0 ? (
          Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="log-line">
              <div className="log-rail"><span className="log-dot dot-blue" /><span className="log-line-v" /></div>
              <div style={{ flex: 1 }}><div className="skel skel-line" /></div>
            </div>
          ))
        ) : (
          lines.map((l, i) => (
            <div className="log-line" key={`${l.seq}-${i}`}>
              <div className="log-rail">
                <span className={`log-dot ${l.dot}${i === 0 && !done ? ' live' : ''}`} />
                <span className="log-line-v" />
              </div>
              <div>
                <div className="log-title">{l.title}</div>
                <div className="log-desc">{l.desc}</div>
              </div>
            </div>
          ))
        )}
        {error != null && (
          <div className="log-desc" style={{ color: 'var(--amber-4)', marginTop: 6 }}>
            SSE 不可用，已降级为轮询
          </div>
        )}
        <div style={{ marginTop: 10 }}>
          <a className="sec-link" href="/tasks">查看完整执行过程 →</a>
        </div>
      </div>
    </div>
  )
}

type LogLine = { seq: number; dot: string; title: string; desc: string }

function toLogLine(e: RunEventPayload): LogLine {
  const d = (e.data ?? {}) as Record<string, unknown>
  const asStr = (v: unknown) => (v == null ? '' : String(v))
  switch (e.type) {
    case 'run.started':
      return { seq: e.seq, dot: 'dot-blue', title: '任务已接收', desc: asStr(d.intent) }
    case 'run.step':
      return { seq: e.seq, dot: 'dot-blue', title: asStr(d.note) || `步骤 ${asStr(d.index)}`, desc: `${asStr(d.tool)} · 执行中` }
    case 'run.step.done':
      return { seq: e.seq, dot: 'dot-emerald', title: `步骤完成 · ${asStr(d.tool)}`, desc: `状态 ${asStr(d.status)}` }
    case 'run.error':
      return { seq: e.seq, dot: 'dot-red', title: '执行出错', desc: asStr(d.message) }
    case 'replan':
      return { seq: e.seq, dot: 'dot-amber', title: '触发重规划', desc: asStr(d.reason) || '自动重试' }
    case 'run.completed':
      return { seq: e.seq, dot: 'dot-emerald', title: '任务已完成', desc: `调用 ${asStr(d.steps)} 个步骤 · 经验已沉淀` }
    case 'run.failed':
      return { seq: e.seq, dot: 'dot-red', title: '任务失败', desc: asStr(d.outcome) }
    default:
      return { seq: e.seq, dot: 'dot-blue', title: e.type, desc: '' }
  }
}

/* ---- 安全与资源概览 ------------------------------------------------------- */

function SecurityOverviewCard() {
  const q = useSecurityOverview()
  const s = q.data
  return (
    <div className="card rail-card">
      <div className="rail-head">
        <span className="t">安全与资源概览</span>
        <a className="sec-link" href="/security">查看详情 →</a>
      </div>
      <div className="rail-body">
        {q.isLoading ? (
          <div className="skel" style={{ height: 60 }} />
        ) : !s ? (
          <div className="empty">无法加载安全概览</div>
        ) : (
          <>
            <div className="sec-status"><i className="dot live" />{s.status}</div>
            <div className="pill-row">
              <span className="pill"><i className="mini" />用户隔离 · {s.isolation_level}</span>
              <span className="pill"><i className="mini" />数据加密 · {s.encryption}</span>
              <span className="pill"><i className="mini" />权限控制 · {s.access_control}</span>
            </div>
            <div className="t-meta" style={{ marginTop: 12 }}>
              策略 {s.policies} · 待审批 {s.pending_approvals} · 高风险拦截 {s.blocked_count}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
