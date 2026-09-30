/**
 * HomeView — AgentFlow home page (PRD §7).
 *
 * INC32 / T04 — the first screen is intentionally minimal: it converges on
 * 「输入任务 → 发起」 (Hero + suggestion chips) plus the 近期任务 rail, and pushes
 * every secondary block (KPI / Agent / Skill / 安全概览) into a single collapsed
 * 第二屏 (`<details data-testid="home-second-screen">`). The old 智能执行日志
 * (`ExecutionLog`) block is removed — the live execution stream now lives on the
 * session workspace (`/tasks`), not on the home page.
 *
 * All blocks render REAL API data (no hardcoded numbers):
 *   · Hero + task input + suggestion chips   ← role-aware (home/roleConfig.ts)
 *   · 近期任务              ← /runs
 *   · 第二屏：4 KPI 卡 / 我的 Agent / 技能中心 / 安全与资源概览
 *
 * KPI #3 「节省成本」 (PRD §7.2-D) is wired to the real /cost/savings contract:
 * it shows the saved `amount` when a baseline exists, and 「—」 otherwise —
 * never a fabricated 0 or a stand-in cost figure. All four KPI values come
 * from `useHomeKpis()` so the view is a pure renderer.
 */

import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import {
  useAgentCatalog,
  useFeaturedSkills,
  useHomeKpis,
  useRecentHubRuns,
  useSecurityOverview,
  useWorkspaceCreateTask,
} from '../api/hooks'
import type { HomeKpis } from '../api/hooks'
import { useSession } from '../hooks/useSession'
import { roleConfigFor } from '../home/roleConfig'
import type { KpiId } from '../home/roleConfig'
import type { PlatformAgent, Skill, RunSummary } from '../api/client'
import {
  IconBot,
  IconChart,
  IconCheck,
  IconClock,
  IconCompass,
  IconCost,
  IconDocument,
  IconList,
  IconPaperclip,
  IconPlus,
  IconSearch,
  IconSend,
  IconShield,
  IconSparkle,
  IconTerminal,
  IconWorkflow,
} from '../components/icons'
import { ResourcePicker } from './runs/ResourcePicker'
import { ModelStatus } from './runs/ModelStatus'
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
      <div className="home home-single">
        <div className="home-main">
          <Hero />
          <RecentTasks />
        </div>
        {/* INC32 / T04 — 第二屏：全部非首屏内容收进一个原生 <details>（默认折叠），
            首屏（1024×768）因此不再出现 KPI / Agent / Skill / 安全概览（AC-6）。
            折展纯由 `<details>` 承载，不新增任何条件渲染，既有 testid 一个不少。 */}
        <details className="home-second" data-testid="home-second-screen">
          <summary className="home-second-summary">
            更多概览 · 指标 / Agent / 技能 / 安全
          </summary>
          <div className="home-second-body">
            <KpiRow />
            <AgentSection />
            <SkillSection />
            <SecurityOverviewCard />
          </div>
        </details>
      </div>
    </section>
  )
}

/* ---- Hero ---------------------------------------------------------------- */

function Hero() {
  const [intent, setIntent] = useState('')
  // INC35 —— 附件行（规格 §3）：已选资源随任务声明为 `context.resources`。后端
  // `_EXPLICIT_INPUT_KEYS` 含 `resources` 且会解引用出真实属性 ⇒ 这是**真**接线，
  // 不是装饰。未选时不传键，保持后端诚实默认（不传空数组）。
  const [resourceIds, setResourceIds] = useState<string[]>([])
  const [attachOpen, setAttachOpen] = useState(false)
  // INC32 / T05 —— 任务创建改走异步通路 `POST /workspace/tasks`
  // （`useWorkspaceCreateTask`）：立即返回句柄、边跑边看；`POST /tasks` 后端不变。
  const create = useWorkspaceCreateTask()
  const session = useSession()
  const role = roleConfigFor(session?.role)
  // INC35 —— 模型只读入口的数据源：最近一次运行（真实）。没有运行过 ⇒ runId 为 null
  // ⇒ `ModelStatus` 显示诚实空态，不伪造一个模型名。
  const latest = useRecentHubRuns(1)
  const latestRunId = latest.data?.items?.[0]?.run_id ?? null
  const busy = create.isPending
  // Read-only roles (viewer) lack `execute:workflows`, so the hero must not
  // offer a submit path that would 403 (see home/roleConfig.ts).
  const canExecute = role.canExecute
  const ready = canExecute && intent.trim().length > 0 && !busy

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!ready) return
    const context: Record<string, unknown> = {}
    if (resourceIds.length > 0) context.resources = resourceIds
    create.mutate(
      {
        intent: intent.trim(),
        context: Object.keys(context).length > 0 ? context : undefined,
      },
      {
        onSuccess: () => {
          setIntent('')
          setResourceIds([])
          setAttachOpen(false)
        },
      },
    )
  }

  return (
    <div className="hero home-hero">
      <div className="hero-inner">
        <h1 className="hero-title">你想让 AI 完成什么？</h1>
        <p className="hero-sub">
          用一句话交代目标，ForgeFlow 自动挑选 Agent、技能与工具
          <span className="text-muted"> · 当前身份：{role.label}</span>
        </p>
        <form className="hero-input" onSubmit={submit}>
          <input
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder={canExecute ? '输入一个任务…' : '当前身份为只读，无法发起任务'}
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
            {/* INC34 — SVG send icon replaces the `➤` text glyph. */}
            {busy ? '…' : <IconSend width={16} height={16} />}
          </button>
        </form>

        {/* INC35 —— 附件行（规格 §3：📎 文件 / ⚡ Skill / ＋ 更多）。三个入口都**真实**：
            「文件」打开资源选择器，选中项随任务声明为 `resources`；
            「技能」跳技能中心（具体选哪个技能由平台决定，见规格 §29）；
            「更多」是原生 `<details>` 菜单，不是假按钮。 */}
        <div className="hero-attach">
          <button
            type="button"
            className={`hero-attach-btn${attachOpen ? ' on' : ''}`}
            onClick={() => setAttachOpen((v) => !v)}
            aria-expanded={attachOpen}
            disabled={!canExecute}
            data-testid="hero-attach-files"
          >
            <IconPaperclip width={13} height={13} />
            文件
            {resourceIds.length > 0 && (
              <span className="hero-attach-count">{resourceIds.length}</span>
            )}
          </button>
          <a className="hero-attach-btn" href="/skills" data-testid="hero-attach-skills">
            <IconSparkle width={13} height={13} />
            技能
          </a>
          <details className="hero-more" data-testid="hero-attach-more">
            <summary className="hero-attach-btn">
              <IconPlus width={13} height={13} />
              更多
            </summary>
            <div className="hero-more-menu">
              <a href="/tasks">继续历史任务</a>
              <a href="/knowledge">知识库</a>
              <a href="/skills">技能中心</a>
            </div>
          </details>
          {/* INC35 —— 模型只读入口（规格 §21/§22）：取**最近一次运行**真实使用的
              Provider / 模型；没有运行过则诚实显示「模型状态未知」，不猜。 */}
          <ModelStatus runId={latestRunId} />
        </div>

        {attachOpen && canExecute && (
          <div className="hero-attach-panel" data-testid="hero-attach-panel">
            <ResourcePicker
              selectedIds={resourceIds}
              onToggle={(id) =>
                setResourceIds((prev) =>
                  prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
                )
              }
              onRegistered={(record) =>
                setResourceIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]))
              }
            />
          </div>
        )}

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

/* INC34 轮2 — KPI 装饰图标全部换成 SVG 线性图标（原 `◎/✓/¥/◷` 文本符号）。
 * 注意：`:259` 金额里的 `¥`（`¥${amount}`）是货币符号，原样保留。 */
const KPI_META: Record<KpiId, { label: string; icon: ReactNode; tone: string }> = {
  total_runs: { label: '总任务数', icon: <IconList width={13} height={13} />, tone: 'ico-blue' },
  success_rate: { label: '成功率', icon: <IconCheck width={13} height={13} />, tone: 'ico-emerald' },
  savings: { label: '节省成本', icon: <IconCost width={13} height={13} />, tone: 'ico-purple' },
  avg_response: { label: '平均响应时间', icon: <IconClock width={13} height={13} />, tone: 'ico-amber' },
}

type KpiView = {
  label: string
  icon: ReactNode
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
  icon: ReactNode
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
        // INC34 — SVG sparkle replaces the `✦` text glyph.
        <div className="card empty"><span className="big"><IconSparkle width={22} height={22} /></span>暂无技能</div>
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
            {/* INC34 轮2 — SVG 图标替换 `◔` 文本符号。 */}
            <span className="big"><IconWorkflow width={22} height={22} /></span>
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
      {/* INC34 轮2 — SVG 图标替换 `◈` 文本符号。 */}
      <span className={`t-ico ${tone}`} aria-hidden="true"><IconWorkflow width={12} height={12} /></span>
      <div className="t-main">
        <div className="t-title">{run.title || run.intent}</div>
        <div className="t-meta">{relativeTime(run.created_at)}</div>
      </div>
      {/* INC34 轮2 — 状态徽标里的 `●` 改用既有 `.dot` 原子（tokens.css）。 */}
      {done ? (
        <span className="badge emerald"><i className="dot" style={{ background: 'var(--emerald-4)' }} />已完成</span>
      ) : run.status === 'failed' ? (
        <span className="badge red"><i className="dot" style={{ background: 'var(--red-4)' }} />失败</span>
      ) : (
        <span className="badge blue"><i className="dot" style={{ background: 'var(--blue-4)' }} />进行中</span>
      )}
    </div>
  )
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
