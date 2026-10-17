/**
 * HomeView — AgentFlow home page（INC43 / T01：从「任务控制台」重构为 Chat-first
 * Agent Workspace）。
 *
 * 首屏结构（对齐截图界面一）：
 *   · `ChatWorkspace` —— Agent 头像行 + 欢迎气泡 + 消息流（用户/Agent turn + 内联
 *     执行轨迹 + Artifact chip）；
 *   · `ChatComposer`  —— 底部富输入区（添加文件 / Skill / 工具 + 输入 + 发送）。
 * 历史对话统一从侧栏或最近对话进入，同一会话支持继续提问。
 * 首页继续保留：
 *   · `<details data-testid="home-second-screen">`（KPI / 智能体 / 技能 / 安全概览）。
 *
 * 旧 `Hero`（h1 / input / chips）主体由 `ChatComposer` 取代；其附件行 / 附件面板
 * 迁入 `ChatComposer`（`hero-attach-*` 四个字面量保留）。
 *
 * 全部块渲染**真实 API 数据**（无硬编码数字）。
 */

import { useEffect, useState } from 'react'
import { useParams } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import {
  useAgentCatalog,
  useFeaturedSkills,
  useHomeKpis,
  useSecurityOverview,
} from '../api/hooks'
import type { HomeKpis } from '../api/hooks'
import { useSession } from '../hooks/useSession'
import { roleConfigFor } from '../home/roleConfig'
import type { KpiId } from '../home/roleConfig'
import type { PlatformAgent, Skill } from '../api/client'
import {
  IconBot,
  IconChart,
  IconCheck,
  IconClock,
  IconCompass,
  IconCost,
  IconDocument,
  IconList,
  IconSearch,
  IconShield,
  IconSparkle,
  IconTerminal,
} from '../components/icons'
// INC43 / T01 —— 首页首屏的 Chat-first 聊天区 + 富输入区。
import { ChatWorkspace } from './home/ChatWorkspace'
import { ChatComposer } from './home/ChatComposer'
import { conversationStorageKey, useChatThread } from './home/useChatThread'
import { NEW_CHAT_EVENT } from './home/navigation'
// AgentFlow 2.0 / Phase B / T-B01 —— 三栏「Agent 工作台」外壳（左=上下文 / 中=对话 /
// 右=对象 Inspector）。中央列仍是既有 ChatWorkspace + ChatComposer（数据源与行为不变）。
import { WorkbenchShell } from './home/WorkbenchShell'
import { ContextPanel } from './home/ContextPanel'
import { ObjectInspector } from './home/ObjectInspector'
import { useSessionObjects } from './home/useSessionObjects'
import type { InspectObject } from './home/inspect'
// AgentFlow 2.0 / Phase C / T-C01 —— 「Change Set」抽屉：右栏「代码变更集」分支触发打开，
// 与 Inspector 用**同一** `changeSet.ts` 真源；动作复用既有 approve/reject/replan。
import { ChangeSetDrawer } from './home/ChangeSetDrawer'
// INC-INLINE-STREAMING / T05 —— 首页内联会话：就地展开的面板 + 与 /tasks **共用**的合并历史源。
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

export function HomeView() {
  const params = useParams({ strict: false }) as { sessionId?: string; skillId?: string; resourceId?: string }
  const session = useSession()
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const reset = () => {
      sessionStorage.removeItem(conversationStorageKey(session?.userId))
      setRevision((current) => current + 1)
    }
    window.addEventListener(NEW_CHAT_EVENT, reset)
    return () => window.removeEventListener(NEW_CHAT_EVENT, reset)
  }, [session?.userId])
  return <HomeWorkspace key={`${session?.userId}-${params.sessionId ?? ''}-${params.skillId ?? ''}-${params.resourceId ?? ''}-${revision}`} sessionId={params.sessionId} skillId={params.skillId} resourceId={params.resourceId} />
}

function HomeWorkspace({ sessionId, skillId, resourceId }: { sessionId?: string; skillId?: string; resourceId?: string }) {
  const [selectedObject, setSelectedObject] = useState<InspectObject | null>(null)
  // AgentFlow 2.0 / Phase C / T-C01 —— Change Set 抽屉的开合（**组件态、不入 URL**，
  // 沿用 `selectedObject` 既有纪律；刷新即回初始态）。
  const [changeSetOpen, setChangeSetOpen] = useState(false)
  // INC43 / T01 —— 首页聊天会话态（提交 → user/agent turn → 单 run 单 SSE）；**不跳页**。
  const thread = useChatThread(sessionId, skillId, resourceId)
  // Phase B / T-B05 —— 左栏上下文与右栏 Inspector **统一锚定「当前会话」** =
  // `thread.sessionId`（它是路由参数 / `sessionStorage` 恢复 / 新建会话三者的**唯一出口**）。
  // 这样「从一次任务出发」后三栏共享同一上下文（左=任务对象树 / 中=对话 / 右=对象详情）。
  // 缺省 `null` ⇒ 左栏诚实空态「还没有任务…」。
  const contextSessionId = thread.sessionId
  const { tree } = useSessionObjects(contextSessionId)
  const latestRunId = tree.runs.length > 0 ? tree.runs[tree.runs.length - 1].runId : null
  const inspectedRunId = selectedObject?.kind === 'run' ? selectedObject.id : selectedObject?.runId ?? latestRunId
  return (
    <section className="view active workspace-view" data-screen-label="首页">
      <div className={`home home-single${thread.turns.length ? ' has-conversation' : ''}`}>
        <div className="home-main">
          {/* AgentFlow 2.0 / Phase B —— 三栏外壳：中央列仍是既有 ChatWorkspace +
              ChatComposer（**组件与数据源不变**），左栏=会话上下文、右栏=对象 Inspector。 */}
          <WorkbenchShell
            inspectorOpen={selectedObject !== null}
            left={
              <ContextPanel
                sessionId={contextSessionId}
                selected={selectedObject}
                onSelect={setSelectedObject}
              />
            }
            center={
              <>
                <ChatWorkspace thread={thread} onInspect={setSelectedObject} />
                <ChatComposer
                  skillId={skillId}
                  initialResourceId={resourceId}
                  onSubmit={thread.submit}
                  busy={thread.busy}
                  draft={thread.draft}
                  onDraftChange={thread.setDraft}
                  canExecute={thread.canExecute}
                  running={thread.running}
                  stopping={thread.stopping}
                  onStop={thread.stop}
                />
              </>
            }
            right={
              <ObjectInspector
                subject={selectedObject}
                runId={inspectedRunId}
                onClose={() => setSelectedObject(null)}
                onOpenChangeSet={() => setChangeSetOpen(true)}
              />
            }
          />
        </div>
        {/* AgentFlow 2.0 / Phase C / T-C01 —— Change Set 抽屉（在工作台之上覆盖；
            数据 + 动作经 `useChangeSet` 只调既有端点；无新导航、无新端点）。 */}
        <ChangeSetDrawer
          runId={inspectedRunId}
          open={changeSetOpen}
          onClose={() => setChangeSetOpen(false)}
        />
        {/* INC32 / T04 — 第二屏：全部非首屏内容收进一个原生 <details>（默认折叠）。 */}
        <details className="home-second" data-testid="home-second-screen">
          <summary className="home-second-summary">
            更多概览 · 指标 / 智能体 / 技能 / 安全
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
          <div className="sec-title">我的智能体</div>
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
        <div className="card empty"><span className="big"><IconBot width={22} height={22} /></span>暂无智能体</div>
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

/* ---- 近期对话 ------------------------------------------------------------- */

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
