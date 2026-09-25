import { useEffect, useMemo } from 'react'
import { Link } from '@tanstack/react-router'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import '../styles/architecture.css'

export function ArchitecturePage() {
  useDocumentTitle('Architecture — reference topology')
  useEffect(() => {
    document.body.classList.add('architecture')
    return () => document.body.classList.remove('architecture')
  }, [])

  return (
    <>
      <ArchTopbar />
      <div className="arch-page">
        <PageHeader />
        <Section01 />
        <Section02 />
        <Section03 />
        <Section04 />
        <Section05 />
        <Section06 />
        <Section07 />
        <Section08 />
        <Endnote />
      </div>
    </>
  )
}

function ArchTopbar() {
  return (
    <header className="arch-topbar">
      <div className="inner">
        <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
          <Link to="/" className="brand">
            <span className="brand-mark" />
            <span className="brand-name">ForgeFlow</span>
          </Link>
          <ul>
            <li><a href="/design-hub">索引</a></li>
            <li><a href="/">落地页</a></li>
            <li><a href="/console">控制台</a></li>
            <li><a href="/architecture" className="active">架构</a></li>
            <li><a href="/docs">文档</a></li>
            <li><a href="/design-system">设计系统</a></li>
          </ul>
        </div>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)' }}>
          v0.1.0 · 预发布
        </div>
      </div>
    </header>
  )
}

function PageHeader() {
  return (
    <div className="top">
      <div>
        <div className="section-eyebrow">系统</div>
        <h1 className="page-h1" style={{ marginTop: 8 }}>一次工作流的解剖。</h1>
        <p className="lede">
          ForgeFlow 运行时的八幅可视化图 —— 从单次 Supervisor 决策到多区域 Kubernetes 部署。
          可将其作为安全审查、平台入职与容量规划的参考架构。
        </p>
        <p
          role="note"
          style={{
            marginTop: 14,
            padding: '8px 12px',
            borderLeft: '2px solid var(--amber-4)',
            background: 'var(--bg-inset)',
            borderRadius: 6,
            fontSize: 12.5,
            color: 'var(--fg-secondary)',
            maxWidth: '72ch',
          }}
        >
          <b style={{ color: 'var(--amber-4)' }}>参考架构。</b>本页的数字、节点数与延迟均为示意 ——
          是目标拓扑，而非来自运行中集群的实时遥测。多区域故障转移与气隙隔离区描述的是部署模式，
          而非托管服务。
        </p>
      </div>
      <div style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)', lineHeight: 1.6 }}>
        8 个章节<br />
        LangGraph · MCP · A2A · pgvector<br />
        Apache 2.0
      </div>
    </div>
  )
}

function SectionHeader({ num, title, sub }: { num: string; title: string; sub: string }) {
  return (
    <div className="h">
      <span className="num">{num}</span>
      <h2>{title}</h2>
      <span className="sub" style={{ marginLeft: 'auto' }}>{sub}</span>
    </div>
  )
}

/* ── 01 · Supervisor orchestration ───────────────────────────────────── */
function Section01() {
  return (
    <section className="block">
      <SectionHeader num="01" title="Supervisor 编排" sub="中心辐射式 · 结构化路由 · LangGraph" />
      <div className="pair">
        <div className="canvas">
          <div className="meta-strip">
            <span>StateGraph · sales_ops · 6 节点 · 14 边</span>
            <span style={{ display: 'flex', gap: 14 }}>
              <LegendSwatch color="var(--blue-4)" label="supervisor" />
              <LegendSwatch color="var(--fg-muted)" label="worker" />
              <LegendSwatch color="var(--amber-4)" label="中断" />
            </span>
          </div>
          <svg viewBox="0 0 800 480" width="100%" height={480} className="diagram-grid" role="img" aria-label="Supervisor 编排：一张中心辐射式 StateGraph，由一个 Supervisor 将工作路由到 researcher、analyzer 与 executor 三类 Worker，并在执行前设有人工审批中断。每次状态转换都会写入检查点。">
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                <path d="M0,0 L10,5 L0,10 z" fill="var(--fg-muted)" />
              </marker>
              <marker id="arrow-blue" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                <path d="M0,0 L10,5 L0,10 z" fill="var(--blue-4)" />
              </marker>
            </defs>
            <g transform="translate(40 200)">
              <circle cx="20" cy="20" r="18" fill="var(--bg-page)" stroke="var(--border-default)" strokeWidth="1.4" />
              <text x="20" y="24" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)" letterSpacing="1">开始</text>
            </g>
            <g transform="translate(330 180)">
              <rect width="140" height="80" rx="14" className="node accent-blue" />
              <text x="70" y="22" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">SUPERVISOR</text>
              <text x="70" y="42" textAnchor="middle" fontFamily="var(--font-sans)" fontSize="14" fill="var(--fg-primary)" fontWeight="500">router</text>
              <text x="70" y="58" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">RoutingDecision</text>
              <text x="70" y="72" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">结构化输出</text>
            </g>
            <WorkerNode y={60} accent="accent-purple" color="var(--purple-4)" name="researcher" sub="web_search · 抓取" />
            <WorkerNode y={160} accent="accent-emerald" color="var(--emerald-4)" name="analyzer" sub="结构化(LeadScore)" />
            <WorkerNode y={260} accent="accent-amber" color="var(--amber-4)" name="executor" sub="CRM · 邮件 · 写入" />
            <g transform="translate(600 360)">
              <rect width="160" height="60" rx="10" className="node accent-amber" strokeDasharray="5 4" />
              <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill="var(--amber-4)" letterSpacing="2">中断</text>
              <text x="14" y="36" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">human_approval</text>
              <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">interrupt_before</text>
            </g>
            <g transform="translate(40 420)">
              <circle cx="20" cy="20" r="18" fill="var(--bg-page)" stroke="var(--emerald-3)" strokeWidth="1.4" />
              <text x="20" y="24" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="10" fill="var(--emerald-4)" letterSpacing="1">结束</text>
            </g>
            <g fill="none" stroke="var(--fg-muted)" strokeWidth="1.2">
              <path d="M 80 220 C 200 220, 240 220, 330 220" markerEnd="url(#arrow)" />
              <path d="M 470 200 C 530 130, 560 100, 600 90" markerEnd="url(#arrow)" />
              <path d="M 470 220 C 540 200, 560 195, 600 190" markerEnd="url(#arrow)" />
              <path d="M 470 240 C 540 270, 560 285, 600 290" markerEnd="url(#arrow)" />
              <path d="M 470 250 C 520 350, 560 385, 600 390" markerEnd="url(#arrow)" strokeDasharray="4 3" />
              <path d="M 600 80 C 510 80, 470 130, 470 180" stroke="var(--blue-3)" opacity="0.5" markerEnd="url(#arrow-blue)" />
              <path d="M 600 180 C 510 170, 480 180, 470 200" stroke="var(--blue-3)" opacity="0.5" markerEnd="url(#arrow-blue)" />
              <path d="M 600 280 C 510 270, 480 250, 470 240" stroke="var(--blue-3)" opacity="0.5" markerEnd="url(#arrow-blue)" />
              <path d="M 400 260 C 300 380, 200 430, 80 438" markerEnd="url(#arrow)" stroke="var(--emerald-3)" opacity="0.6" />
            </g>
            <text x="200" y="210" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">user_payload</text>
            <text x="510" y="180" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)">路由(stage)</text>
          </svg>
          <div className="legend-row">
            <span><span className="solid" style={{ color: 'var(--fg-muted)' }} /> 前向路由</span>
            <span><span className="solid" style={{ color: 'var(--blue-4)' }} /> 返回 Supervisor</span>
            <span><span className="dashed" style={{ color: 'var(--fg-muted)' }} /> 可中断边</span>
            <span style={{ marginLeft: 'auto' }}>每次转换都持久化检查点</span>
          </div>
        </div>
        <div className="copy">
          <h3>由 Supervisor 统一决策，图保持确定性。</h3>
          <p>
            每一步，Supervisor 都会读取当前状态并输出一个带类型的{' '}
            <span className="mono" style={{ color: 'var(--blue-4)' }}>RoutingDecision = {`{next: Worker, reasoning: str}`}</span>。
            条件边消费该决策并选择下一个节点 —— 无需字符串解析，也不会出现「Agent 自作主张」。
          </p>
          <p>
            Worker 执行、修改状态并返回，随后 Supervisor 再次运行。可中断节点会干净地暂停图，
            并从某个 token 处恢复 —— 没有内存中续跑之类的取巧手段。
          </p>
          <KvTable rows={[
            ['路由模型', 'gpt-4o · structured_output'],
            ['状态模式', 'WorkflowState · Pydantic'],
            ['检查点', 'postgres://checkpoints/wf_*'],
            ['恢复', '任意 Worker · 任意区域 · 任意 Pod'],
            ['p50 路由延迟', '412ms'],
          ]} />
        </div>
      </div>
    </section>
  )
}

function WorkerNode({ y, accent, color, name, sub }: { y: number; accent: string; color: string; name: string; sub: string }) {
  return (
    <g transform={`translate(600 ${y})`}>
      <rect width="160" height="60" rx="10" className={`node ${accent}`} />
      <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill={color} letterSpacing="2">WORKER</text>
      <text x="14" y="36" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">{name}</text>
      <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{sub}</text>
    </g>
  )
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <span>
      <i style={{ display: 'inline-block', width: 8, height: 8, background: color, borderRadius: '50%', marginRight: 4 }} />
      {label}
    </span>
  )
}

function KvTable({ rows }: { rows: [string, string][] }) {
  return (
    <table className="kv" style={{ marginTop: 14 }}>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}><td>{k}</td><td>{v}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

/* ── 02 · A2A communication ──────────────────────────────────────────── */
function Section02() {
  return (
    <section className="block">
      <SectionHeader num="02" title="A2A 通信" sub="JSON-RPC 2.0 · 能力发现 · Worker 直连" />
      <div className="pair">
        <div className="copy">
          <h3>Worker 可以绕过 Supervisor 直接对话。</h3>
          <p>
            对于延迟敏感的协作 —— 在 researcher 与 analyzer 之间传递富化结果、广播共享查询 ——
            Worker 之间通过 JSON-RPC 2.0 交换 A2A 消息。每个 Agent 发布一张{' '}
            <span className="mono" style={{ color: 'var(--purple-4)' }}>AgentCard</span> 描述自身能力；
            注册中心将 <span className="mono">capability/v</span> 解析到实时端点。
          </p>
          <p>工作流状态与路由仍由 Supervisor 掌管。A2A 用于旁路协作，而非控制流。</p>
          <KvTable rows={[
            ['线格式', 'JSON-RPC 2.0 over HTTP/2'],
            ['发现', 'AgentCard · capability/v1.json'],
            ['认证', 'mTLS · 工作负载身份'],
            ['追踪', 'OTel · W3C traceparent'],
            ['背压', '每对端令牌桶'],
          ]} />
        </div>
        <div className="canvas">
          <div className="meta-strip">
            <span>A2A 注册中心 · 14 张卡片 · 38 项能力</span>
            <span>JSON-RPC 2.0</span>
          </div>
          <svg viewBox="0 0 520 420" width="100%" height={420} className="diagram-grid" role="img" aria-label="Agent 之间通信：Worker 向中央注册中心登记能力卡片，并通过 JSON-RPC 2.0 交换点对点消息，而工作流状态与路由仍由 Supervisor 掌管。">
            <g transform="translate(220 180)">
              <rect width="100" height="60" rx="10" className="node accent-purple" />
              <text x="50" y="20" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--purple-4)" letterSpacing="2">注册中心</text>
              <text x="50" y="40" textAnchor="middle" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">a2a.svc</text>
              <text x="50" y="54" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="8" fill="var(--fg-muted)">14 张卡片</text>
            </g>
            <A2AAgent x={40} y={60} accent="accent-purple" color="var(--purple-4)" name="researcher" cap="research/v2" />
            <A2AAgent x={360} y={60} accent="accent-emerald" color="var(--emerald-4)" name="analyzer" cap="score/v3" />
            <A2AAgent x={40} y={320} accent="accent-purple" color="var(--purple-4)" name="enricher" cap="enrich/v1" />
            <A2AAgent x={360} y={320} accent="accent-amber" color="var(--amber-4)" name="executor" cap="execute/v3" />
            <g fill="none" stroke="var(--fg-muted)" strokeWidth="1" strokeDasharray="2 4" opacity="0.6">
              <path d="M 160 84 C 200 84, 240 140, 270 180" />
              <path d="M 360 84 C 320 90, 290 140, 270 180" />
              <path d="M 160 344 C 220 320, 240 250, 270 240" />
              <path d="M 360 344 C 320 320, 290 250, 270 240" />
            </g>
            <text x="180" y="155" fontFamily="var(--font-mono)" fontSize="8" fill="var(--fg-muted)">注册</text>
            <g fill="none" stroke="var(--purple-3)" strokeWidth="1.6">
              <path d="M 160 84 C 240 50, 320 50, 360 84" />
              <path d="M 160 344 C 240 380, 320 380, 360 344" />
              <path d="M 100 108 C 60 200, 60 280, 100 320" />
              <path d="M 420 108 C 460 200, 460 280, 420 320" />
            </g>
            <g>
              <rect width="22" height="10" rx="2" fill="var(--purple-4)" opacity="0.85">
                <animateMotion dur="2.4s" repeatCount="indefinite" path="M 160 84 C 240 50, 320 50, 360 84" />
              </rect>
              <rect width="22" height="10" rx="2" fill="var(--purple-4)" opacity="0.85">
                <animateMotion dur="2.6s" repeatCount="indefinite" begin="0.8s" path="M 360 344 C 320 380, 240 380, 160 344" />
              </rect>
              <rect width="22" height="10" rx="2" fill="var(--purple-4)" opacity="0.85">
                <animateMotion dur="3.0s" repeatCount="indefinite" begin="1.4s" path="M 100 108 C 60 200, 60 280, 100 320" />
              </rect>
            </g>
            <g transform="translate(180 130)">
              <rect width="180" height="42" rx="4" fill="var(--bg-overlay)" stroke="var(--border-default)" />
              <text x="10" y="14" fontFamily="var(--font-mono)" fontSize="9" fill="var(--purple-4)" letterSpacing="2">PAYLOAD · 2.1KB</text>
              <text x="10" y="28" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">capability: score_lead/v1</text>
              <text x="10" y="40" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">trace: 9F1C…E08A</text>
            </g>
          </svg>
          <div className="legend-row">
            <span><span className="dashed" style={{ color: 'var(--fg-muted)' }} /> 注册 / 查询</span>
            <span><span className="solid" style={{ color: 'var(--purple-4)' }} /> 点对点消息</span>
            <span style={{ marginLeft: 'auto' }}>平均 64ms · p99 184ms</span>
          </div>
        </div>
      </div>
    </section>
  )
}

function A2AAgent({ x, y, accent, color, name, cap }: { x: number; y: number; accent: string; color: string; name: string; cap: string }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect width="120" height="48" rx="8" className={`node ${accent}`} />
      <text x="10" y="16" fontFamily="var(--font-mono)" fontSize="8" fill={color} letterSpacing="2">AGENT</text>
      <text x="10" y="30" fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">{name}</text>
      <text x="10" y="42" fontFamily="var(--font-mono)" fontSize="8" fill="var(--fg-muted)">{cap}</text>
    </g>
  )
}

/* ── 03 · MCP tool topology ──────────────────────────────────────────── */
const MCP_TOOLS_LEFT = [
  { name: 'tavily.web_search', ver: 'v2' },
  { name: 'browser.scrape_url', ver: 'v1' },
  { name: 'salesforce.write', ver: 'v3' },
  { name: 'crm.stage', ver: 'v3' },
  { name: 'smtp.send', ver: 'v1' },
]
const MCP_TOOLS_RIGHT = [
  { name: 'memory.recall', ver: 'internal' },
  { name: 'memory.store', ver: 'internal' },
  { name: 'snowflake.query', ver: 'v4' },
  { name: 'jira.create_issue', ver: 'v2' },
  { name: 'slack.notify', ver: 'v2' },
]

function Section03() {
  return (
    <section className="block">
      <SectionHeader num="03" title="MCP 工具拓扑" sub="streamable-HTTP · 能力发现 · 更换 Provider" />
      <div className="canvas">
        <div className="meta-strip">
          <span>FastMCP :8001 · 14 个工具 · 4 个 Provider · 8,124 次调用/分钟</span>
          <span>streamable-HTTP</span>
        </div>
        <svg viewBox="0 0 1280 360" width="100%" height={360} className="diagram-grid" role="img" aria-label="MCP 工具拓扑：Agent 通过单一 FastMCP 服务器调用工具，该服务器以 streamable-HTTP 线格式对接 SaaS、数据与本地部署三类 Provider，因此更换 Provider 无需重新部署 Agent。">
          <g fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)" letterSpacing="2">
            <text x="20" y="36">AGENT</text>
            <text x="500" y="36">MCP SERVER</text>
            <text x="900" y="36">PROVIDER</text>
          </g>
          <McpAgent y={60} accent="accent-purple" name="researcher" sub="2 个工具" />
          <McpAgent y={120} accent="accent-emerald" name="analyzer" sub="1 个工具" />
          <McpAgent y={180} accent="accent-amber" name="executor" sub="4 个工具" />
          <McpAgent y={240} accent="accent-purple" name="enricher" sub="3 个工具" />
          <g transform="translate(420 60)">
            <rect width="440" height="240" rx="12" fill="var(--bg-canvas)" stroke="var(--border-default)" strokeWidth="1.2" />
            <text x="20" y="22" fontFamily="var(--font-mono)" fontSize="10" fill="var(--blue-4)" letterSpacing="2">MCP SERVER · FastMCP</text>
            <text x="20" y="36" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">已注册工具：14</text>
            {MCP_TOOLS_LEFT.map((t, i) => (
              <ToolRow key={t.name} x={20} y={58 + i * 32} width={180} tool={t} />
            ))}
            {MCP_TOOLS_RIGHT.map((t, i) => (
              <ToolRow key={t.name} x={220} y={58 + i * 32} width={200} tool={t} />
            ))}
          </g>
          <McpProvider x={900} y={60} accent="accent-blue" color="var(--blue-4)" eyebrow="SAAS" name="Tavily · Salesforce" sub="Snowflake · Jira · Slack" />
          <McpProvider x={900} y={140} accent="accent-emerald" color="var(--emerald-4)" eyebrow="数据" name="Postgres · pgvector" sub="记忆 · 检查点" />
          <McpProvider x={900} y={220} accent="accent-amber" color="var(--amber-4)" eyebrow="本地部署" name="SMTP · 内部 CRM" sub="位于 VPC 内" />
          <g fill="none" stroke="var(--fg-muted)" strokeWidth="1" opacity="0.6">
            <path d="M 180 80 C 280 80, 320 80, 420 100" />
            <path d="M 180 140 C 280 140, 320 130, 420 130" />
            <path d="M 180 200 C 280 180, 320 180, 420 160" />
            <path d="M 180 260 C 280 240, 320 240, 420 220" />
            <path d="M 860 90 C 880 90, 880 90, 900 90" />
            <path d="M 860 130 C 880 140, 880 160, 900 160" />
            <path d="M 860 200 C 880 220, 880 240, 900 240" />
          </g>
          <g>
            <circle r="3" fill="var(--blue-4)">
              <animateMotion dur="1.8s" repeatCount="indefinite" path="M 180 80 C 280 80, 320 80, 420 100" />
            </circle>
            <circle r="3" fill="var(--amber-4)">
              <animateMotion dur="2.2s" repeatCount="indefinite" begin="0.3s" path="M 180 200 C 280 180, 320 180, 420 160" />
            </circle>
          </g>
        </svg>
        <div className="legend-row">
          <span>发现 · 调用 · 流式 —— 全部 14 个工具共用同一套线格式</span>
          <span style={{ marginLeft: 'auto' }}>无需重新部署任何 Agent，即可将 tavily 换为 bing</span>
        </div>
      </div>
    </section>
  )
}

function McpAgent({ y, accent, name, sub }: { y: number; accent: string; name: string; sub: string }) {
  return (
    <g transform={`translate(20 ${y})`}>
      <rect width="160" height="44" rx="8" className={`node ${accent}`} />
      <text x="14" y="18" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">{name}</text>
      <text x="14" y="34" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{sub}</text>
    </g>
  )
}

function ToolRow({ x, y, width, tool }: { x: number; y: number; width: number; tool: { name: string; ver: string } }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect width={width} height="26" rx="5" fill="var(--bg-elevated)" stroke="var(--border-subtle)" />
      <text x="10" y="18" fontFamily="var(--font-mono)" fontSize="11" fill="var(--fg-primary)">{tool.name}</text>
      <text x={width - 8} y="18" textAnchor="end" fontSize="9" fill="var(--fg-muted)">{tool.ver}</text>
    </g>
  )
}

function McpProvider({ x, y, accent, color, eyebrow, name, sub }: { x: number; y: number; accent: string; color: string; eyebrow: string; name: string; sub: string }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect width="180" height="60" rx="8" className={`node ${accent}`} />
      <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill={color} letterSpacing="2">{eyebrow}</text>
      <text x="14" y="36" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)">{name}</text>
      <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{sub}</text>
    </g>
  )
}

/* ── 04 · Semantic memory graph ──────────────────────────────────────── */
// Deterministic cluster point cloud (replaces document.write random scatter).
function makeClusterPoints(seed: number) {
  // Mulberry32 PRNG — tiny, deterministic, plenty for visual scatter.
  let s = seed >>> 0
  function rnd() {
    s |= 0; s = (s + 0x6D2B79F5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  const clusters = [
    { cx: 200, cy: 160, r: 90, color: 'var(--blue-4)', n: 120 },
    { cx: 510, cy: 190, r: 80, color: 'var(--purple-4)', n: 90 },
    { cx: 280, cy: 350, r: 100, color: 'var(--emerald-4)', n: 110 },
    { cx: 540, cy: 370, r: 70, color: 'var(--amber-4)', n: 70 },
  ]
  const out: { x: number; y: number; color: string }[] = []
  for (const cl of clusters) {
    for (let i = 0; i < cl.n; i++) {
      const a = rnd() * Math.PI * 2
      const r = Math.pow(rnd(), 0.6) * cl.r
      out.push({ x: cl.cx + Math.cos(a) * r, y: cl.cy + Math.sin(a) * r, color: cl.color })
    }
  }
  return out
}

function Section04() {
  const points = useMemo(() => makeClusterPoints(42), [])
  return (
    <section className="block">
      <SectionHeader num="04" title="语义记忆图" sub="pgvector · ivfflat · 命名空间隔离 · 余弦" />
      <div className="pair">
        <div className="canvas">
          <div className="meta-strip">
            <span>记忆 · 1.4M 向量 · 1536 维 · ns:sales/*</span>
            <span>UMAP 投影</span>
          </div>
          <svg viewBox="0 0 720 460" width="100%" height={460} className="diagram-grid" role="img" aria-label="语义记忆：嵌入向量以 pgvector 存于 Postgres，并按命名空间投影为聚类。查询会按余弦相似度返回最近的命名空间范围内匹配项。">
            <g opacity="0.12">
              <ellipse cx="200" cy="160" rx="140" ry="100" fill="var(--blue-4)" />
              <ellipse cx="510" cy="190" rx="130" ry="95" fill="var(--purple-4)" />
              <ellipse cx="280" cy="350" rx="160" ry="80" fill="var(--emerald-4)" />
              <ellipse cx="540" cy="370" rx="110" ry="65" fill="var(--amber-4)" />
            </g>
            <g fontFamily="var(--font-mono)" fontSize="11" fill="var(--fg-secondary)">
              <text x="120" y="70">ns:sales/stripe</text>
              <text x="450" y="100">ns:sales/vercel</text>
              <text x="190" y="436">ns:policy/global</text>
              <text x="478" y="436">ns:support/*</text>
            </g>
            <g>
              {points.map((p, i) => (
                <circle key={i} cx={p.x.toFixed(1)} cy={p.y.toFixed(1)} r="1.8" fill={p.color} opacity="0.7" />
              ))}
            </g>
            <g>
              <circle cx="240" cy="140" r="9" fill="none" stroke="var(--fg-primary)" strokeWidth="1.6" />
              <circle cx="240" cy="140" r="4" fill="var(--fg-primary)" />
              <line x1="240" y1="140" x2="208" y2="156" stroke="var(--fg-primary)" strokeDasharray="2 3" opacity="0.5" />
              <line x1="240" y1="140" x2="172" y2="148" stroke="var(--fg-primary)" strokeDasharray="2 3" opacity="0.5" />
              <line x1="240" y1="140" x2="226" y2="184" stroke="var(--fg-primary)" strokeDasharray="2 3" opacity="0.5" />
              <circle cx="208" cy="156" r="3.4" fill="var(--fg-primary)" stroke="var(--bg-page)" strokeWidth="1" />
              <circle cx="172" cy="148" r="3.4" fill="var(--fg-primary)" stroke="var(--bg-page)" strokeWidth="1" />
              <circle cx="226" cy="184" r="3.4" fill="var(--fg-primary)" stroke="var(--bg-page)" strokeWidth="1" />
              <g transform="translate(260 130)">
                <rect width="160" height="46" rx="4" fill="var(--bg-overlay)" stroke="var(--border-default)" />
                <text x="10" y="14" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">查询 · k=3</text>
                <text x="10" y="28" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">"Q2 2026 扩张交易"</text>
                <text x="10" y="40" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">cos: 0.89 · 0.84 · 0.78</text>
              </g>
            </g>
            <g stroke="var(--fg-faint)" strokeWidth="0.6" strokeDasharray="1 3" opacity="0.6">
              <line x1="240" y1="140" x2="500" y2="180" />
              <line x1="240" y1="140" x2="290" y2="340" />
            </g>
          </svg>
          <div className="legend-row">
            <span>ivfflat · lists=200 · probes=10</span>
            <span style={{ marginLeft: 'auto' }}>p50 召回：38ms · p99：92ms</span>
          </div>
        </div>
        <div className="copy">
          <h3>记忆属于工作负载，而非 LLM。</h3>
          <p>
            嵌入向量与其余事务数据一并存于 Postgres —— 无需额外服务，也无需向安全团队解释一致性方案。
            每条记录都限定在某个命名空间内，因此工作流不会误召回彼此的数据。
          </p>
          <p>
            召回只是一次工具调用：任意 Agent 通过 MCP 调用{' '}
            <span className="mono" style={{ color: 'var(--blue-4)' }}>memory.recall(ns, q, k)</span>，
            即可获得带类型、带引用的匹配结果。
          </p>
          <KvTable rows={[
            ['索引', 'ivfflat · 余弦 · 1536 维'],
            ['租户隔离', '行级安全 · ns 前缀'],
            ['加密', '静态加密 · 每租户 KMS 密钥'],
            ['TTL', '按命名空间策略'],
            ['被遗忘权', '按 trace_id 级联删除'],
          ]} />
        </div>
      </div>
    </section>
  )
}

/* ── 05 · State machine & checkpointing ──────────────────────────────── */
type CheckpointKind = 'start' | 'route' | 'work' | 'recall' | 'tool' | 'pause' | 'now'
const CHECKPOINTS: { x: number; lbl: string; kind: CheckpointKind; stage: string }[] = [
  { x: 60, lbl: '#01', kind: 'start', stage: 'qualify' },
  { x: 140, lbl: '#02', kind: 'route', stage: 'route→research' },
  { x: 220, lbl: '#03', kind: 'work', stage: 'research' },
  { x: 300, lbl: '#04', kind: 'work', stage: 'research' },
  { x: 380, lbl: '#05', kind: 'route', stage: 'route→analyze' },
  { x: 460, lbl: '#06', kind: 'work', stage: 'analyze' },
  { x: 540, lbl: '#07', kind: 'recall', stage: 'memory.recall' },
  { x: 620, lbl: '#08', kind: 'route', stage: 'route→propose' },
  { x: 700, lbl: '#09', kind: 'work', stage: 'propose' },
  { x: 780, lbl: '#10', kind: 'tool', stage: 'crm.stage' },
  { x: 860, lbl: '#11', kind: 'tool', stage: 'draft.email' },
  { x: 940, lbl: '#12', kind: 'route', stage: 'route→approval' },
  { x: 1020, lbl: '#13', kind: 'pause', stage: 'interrupt_before' },
  { x: 1100, lbl: '#14', kind: 'now', stage: 'awaiting' },
]
const KIND_COLOR: Record<CheckpointKind, string> = {
  start: 'var(--blue-4)',
  route: 'var(--blue-4)',
  work: 'var(--purple-4)',
  recall: 'var(--emerald-4)',
  tool: 'var(--amber-4)',
  pause: 'var(--amber-4)',
  now: 'oklch(0.96 0.005 250)',
}

function Section05() {
  return (
    <section className="block">
      <SectionHeader num="05" title="工作流状态与检查点" sub="每个节点都持久化 · 可跨 Pod 恢复 · 可分叉" />
      <div className="canvas">
        <div className="meta-strip">
          <span>StateGraph 转换 · sales_ops</span>
          <span>本次运行已持久化 14 个检查点</span>
        </div>
        <div style={{ padding: '28px 32px' }}>
          <div className="state-row">
            <span className="pill start">qualify</span><span className="arr">→</span>
            <span className="pill">research</span><span className="arr">→</span>
            <span className="pill">analyze</span><span className="arr">→</span>
            <span className="pill">propose</span><span className="arr">→</span>
            <span className="pill pause">await_approval</span><span className="arr">→</span>
            <span className="pill">execute</span><span className="arr">→</span>
            <span className="pill end">done</span>
          </div>
          <svg viewBox="0 0 1200 200" width="100%" height={200} style={{ marginTop: 36 }} role="img" aria-label="工作流状态与检查点：一次运行中已持久化检查点的时间线，从 qualify 起，历经路由、工作、记忆召回与工具调用，在人工审批中断处暂停。任意 Pod 都能从最新检查点恢复。">
            <line x1="60" y1="100" x2="1140" y2="100" stroke="var(--border-default)" strokeWidth="1" />
            {CHECKPOINTS.map((c) => {
              const col = KIND_COLOR[c.kind]
              const isNow = c.kind === 'now'
              return (
                <g key={c.lbl} transform={`translate(${c.x} 100)`}>
                  <line x1="0" y1="-26" x2="0" y2="0" stroke={col} strokeWidth="1.2" opacity="0.5" />
                  <circle cx="0" cy="0" r={isNow ? 6 : 4} fill={col} stroke="var(--bg-canvas)" strokeWidth="2" />
                  {isNow && (
                    <circle cx="0" cy="0" r="10" fill="none" stroke={col} opacity="0.5">
                      <animate attributeName="r" values="6;14;6" dur="1.6s" repeatCount="indefinite" />
                      <animate attributeName="opacity" values="0.5;0;0.5" dur="1.6s" repeatCount="indefinite" />
                    </circle>
                  )}
                  <text x="0" y="-32" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{c.lbl}</text>
                  <text x="0" y="22" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9" fill={col}>{c.stage}</text>
                </g>
              )
            })}
            <text x="1100" y="60" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-primary)" letterSpacing="2">当前</text>
          </svg>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 32, marginTop: 36 }}>
            <ColumnNote eyebrow="可恢复性" body={<>任意 Pod 都能通过最新检查点，借助 <span className="mono" style={{ color: 'var(--blue-4)' }}>run_id</span> 恢复任意运行。冷启动延迟：p50 240ms。</>} />
            <ColumnNote eyebrow="回放与分叉" body="可在任意检查点分叉，针对完全一致的上游状态测试 prompt / 模型改动。回放会生成确定性的转录，用于评估。" />
            <ColumnNote eyebrow="审计链" body="每个检查点都与前一个哈希链式相连，并用该 Pod 的工作负载身份签名 —— 为受监管的工作流提供防篡改的状态。" />
          </div>
        </div>
      </div>
    </section>
  )
}

function ColumnNote({ eyebrow, body }: { eyebrow: string; body: React.ReactNode }) {
  return (
    <div>
      <div className="section-eyebrow">{eyebrow}</div>
      <p style={{ marginTop: 6, color: 'var(--fg-secondary)', fontSize: 13 }}>{body}</p>
    </div>
  )
}

/* ── 06 · Event streaming & observability pipeline ───────────────────── */
function Section06() {
  return (
    <section className="block">
      <SectionHeader num="06" title="事件流与可观测性流水线" sub="Kafka · Redis · LangSmith · OTel" />
      <div className="canvas">
        <div className="meta-strip">
          <span>~1.8k 事件/秒 持续 · 12k 峰值 · 30 天留存</span>
          <span>SSE · Kafka · OTel</span>
        </div>
        <svg viewBox="0 0 1280 280" width="100%" height={280} className="diagram-grid" role="img" aria-label="事件流与可观测性流水线：生产者把事件写入 Kafka 主题，经由 Redis Streams 与服务器发送事件实时扇出到控制台，并通过 OpenTelemetry Collector 导出到追踪、分析、冷存储与 SIEM 等下游。">
          <g transform="translate(40 80)">
            <rect width="160" height="120" rx="10" fill="var(--bg-canvas)" stroke="var(--border-default)" />
            <text x="14" y="20" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">生产者</text>
            {['workflow.node', 'tool.invoke', 'agent.message', 'audit.write', 'cost.tick'].map((p, i) => (
              <text key={p} x="14" y={44 + i * 16} fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">{p}</text>
            ))}
          </g>
          <g transform="translate(300 60)">
            <rect width="280" height="160" rx="12" fill="var(--bg-canvas)" stroke="var(--border-default)" />
            <text x="20" y="22" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">KAFKA · 6 分区</text>
            {[
              { name: 'forge.events.runs', rps: '820/s' },
              { name: 'forge.events.tools', rps: '512/s' },
              { name: 'forge.events.audit', rps: '128/s' },
              { name: 'forge.events.cost', rps: '340/s' },
            ].map((t, i) => (
              <g key={t.name} transform={`translate(20 ${36 + i * 28})`}>
                <rect width="240" height="22" rx="4" fill="var(--bg-elevated)" stroke="var(--border-subtle)" />
                <text x="10" y="15" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-secondary)">{t.name}</text>
                <text x="232" y="15" textAnchor="end" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{t.rps}</text>
              </g>
            ))}
          </g>
          <g transform="translate(640 60)">
            <rect width="180" height="80" rx="10" fill="var(--bg-canvas)" stroke="var(--border-default)" />
            <text x="14" y="20" fontFamily="var(--font-mono)" fontSize="9" fill="var(--red-4)" letterSpacing="2">REDIS STREAMS</text>
            <text x="14" y="40" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">实时扇出</text>
            <text x="14" y="56" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">SSE · UI 订阅</text>
            <text x="14" y="70" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">~1.6k 客户端</text>
          </g>
          <g transform="translate(640 152)">
            <rect width="180" height="68" rx="10" fill="var(--bg-canvas)" stroke="var(--border-default)" />
            <text x="14" y="20" fontFamily="var(--font-mono)" fontSize="9" fill="var(--emerald-4)" letterSpacing="2">OTel COLLECTOR</text>
            <text x="14" y="40" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">otelcol</text>
            <text x="14" y="56" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">Span · 指标 · 日志</text>
          </g>
          <Sink x={880} y={60} accent="accent-blue" name="控制台 UI" sub="实时时间线" />
          <Sink x={880} y={116} accent="accent-purple" name="LangSmith" sub="追踪 · 评估" />
          <Sink x={880} y={172} accent="accent-emerald" name="Datadog · Honeycomb" sub="OTel 输出" />
          <Sink x={1080} y={60} accent="accent-amber" name="S3 冷存储" sub="WORM · 7 年" />
          <Sink x={1080} y={116} accent="accent-red" name="SIEM · Splunk" sub="审计管道" />
          <Sink x={1080} y={172} accent="accent-emerald" name="Postgres · pgvector" sub="分析" />
          <g fill="none" stroke="var(--fg-muted)" strokeWidth="1" opacity="0.55">
            <path d="M 200 110 C 240 110, 270 110, 300 110" />
            <path d="M 200 130 C 240 130, 270 130, 300 130" />
            <path d="M 200 150 C 240 150, 270 150, 300 150" />
            <path d="M 580 90 C 600 90, 620 90, 640 90" />
            <path d="M 580 130 C 600 140, 620 170, 640 180" />
            <path d="M 820 80 C 850 80, 860 80, 880 80" />
            <path d="M 820 100 C 850 110, 860 130, 880 130" />
            <path d="M 820 180 C 850 180, 860 180, 880 180" />
            <path d="M 1040 80 C 1060 80, 1060 80, 1080 80" />
            <path d="M 1040 132 C 1060 130, 1060 130, 1080 130" />
            <path d="M 1040 190 C 1060 190, 1060 190, 1080 190" />
          </g>
          <g>
            <rect width="6" height="6" fill="var(--blue-4)">
              <animateMotion dur="1.6s" repeatCount="indefinite" path="M 200 110 C 280 110, 320 110, 580 110" />
            </rect>
            <rect width="6" height="6" fill="var(--amber-4)">
              <animateMotion dur="2.0s" repeatCount="indefinite" begin="0.4s" path="M 200 130 C 280 130, 320 130, 580 130" />
            </rect>
            <rect width="6" height="6" fill="var(--emerald-4)">
              <animateMotion dur="2.2s" repeatCount="indefinite" begin="0.8s" path="M 200 150 C 280 150, 320 150, 580 150" />
            </rect>
          </g>
        </svg>
      </div>
    </section>
  )
}

function Sink({ x, y, accent, name, sub }: { x: number; y: number; accent: string; name: string; sub: string }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect width="160" height="44" rx="8" className={`node ${accent}`} />
      <text x="14" y="18" fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">{name}</text>
      <text x="14" y="34" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">{sub}</text>
    </g>
  )
}

/* ── 07 · Kubernetes deployment topology ─────────────────────────────── */
type PodKind = 'api' | 'sup' | 'rs' | 'an' | 'ex' | 'mcp' | 'pg' | 'r' | ''
type Node = { name: string; size: string; cpu: string; mem: string; pods: { kind: PodKind; label: string }[] }

const K8S_NODES: Node[] = [
  {
    name: 'node-01 · m6i.4xlarge', size: '', cpu: '64%', mem: '71%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'api', label: 'api' }, { kind: 'api', label: 'api' },
      { kind: 'sup', label: 'sup' }, { kind: 'sup', label: 'sup' },
      { kind: 'rs', label: 'rs' }, { kind: 'an', label: 'an' }, { kind: 'an', label: 'an' },
      { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'mcp', label: 'mcp' },
      { kind: 'pg', label: 'pg' }, { kind: 'pg', label: 'pg' },
      { kind: '', label: 'o11y' }, { kind: '', label: 'otel' },
    ],
  },
  {
    name: 'node-02 · m6i.4xlarge', size: '', cpu: '58%', mem: '64%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'api', label: 'api' },
      { kind: 'sup', label: 'sup' }, { kind: 'sup', label: 'sup' },
      { kind: 'rs', label: 'rs' }, { kind: 'rs', label: 'rs' },
      { kind: 'an', label: 'an' },
      { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'pg', label: 'pg' },
      { kind: '', label: 'kafka' }, { kind: '', label: 'redis' }, { kind: '', label: 'otel' }, { kind: '', label: 'cron' },
    ],
  },
  {
    name: 'node-03 · m6i.4xlarge', size: '', cpu: '61%', mem: '68%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'sup', label: 'sup' },
      { kind: 'rs', label: 'rs' }, { kind: 'rs', label: 'rs' },
      { kind: 'an', label: 'an' }, { kind: 'an', label: 'an' },
      { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'mcp', label: 'mcp' },
      { kind: 'pg', label: 'pg' }, { kind: 'r', label: 'r' },
      { kind: '', label: 'kafka' }, { kind: '', label: 'redis' }, { kind: '', label: 'otel' }, { kind: '', label: 'vault' },
    ],
  },
  {
    name: 'node-04 · m6i.4xlarge', size: '', cpu: '49%', mem: '52%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'sup', label: 'sup' }, { kind: 'rs', label: 'rs' },
      { kind: 'an', label: 'an' }, { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'mcp', label: 'mcp' },
      { kind: 'pg', label: 'pg' },
      { kind: '', label: 'kafka' }, { kind: '', label: 'redis' }, { kind: '', label: 'otel' },
      { kind: '', label: 'vault' }, { kind: '', label: 'dash' }, { kind: '', label: 'cron' }, { kind: '', label: 'jobs' },
    ],
  },
  {
    name: 'node-05 · m6i.4xlarge', size: '', cpu: '71%', mem: '78%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'sup', label: 'sup' },
      { kind: 'rs', label: 'rs' }, { kind: 'rs', label: 'rs' },
      { kind: 'an', label: 'an' }, { kind: 'an', label: 'an' },
      { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'mcp', label: 'mcp' },
      { kind: 'pg', label: 'pg' },
      { kind: '', label: 'kafka' }, { kind: '', label: 'redis' }, { kind: '', label: 'otel' }, { kind: '', label: 'dash' },
    ],
  },
  {
    name: 'node-06 · m6i.4xlarge', size: '', cpu: '54%', mem: '62%',
    pods: [
      { kind: 'api', label: 'api' }, { kind: 'sup', label: 'sup' }, { kind: 'rs', label: 'rs' },
      { kind: 'an', label: 'an' }, { kind: 'an', label: 'an' },
      { kind: 'ex', label: 'ex' }, { kind: 'ex', label: 'ex' },
      { kind: 'mcp', label: 'mcp' }, { kind: 'mcp', label: 'mcp' },
      { kind: 'pg', label: 'pg' },
      { kind: '', label: 'kafka' }, { kind: '', label: 'redis' }, { kind: '', label: 'otel' },
      { kind: '', label: 'dash' }, { kind: '', label: 'jobs' }, { kind: '', label: 'jobs' },
    ],
  },
]

function Section07() {
  return (
    <section className="block">
      <SectionHeader num="07" title="Kubernetes 部署拓扑" sub="3 个命名空间 · HPA · NetworkPolicy · 气隙场景 0 外联" />
      <div className="canvas">
        <div className="meta-strip">
          <span>主生产环境 · k8s 1.30 · 6 个节点 · 128 个 Pod</span>
          <span>Helm Chart · forgeflow-0.1.0</span>
        </div>
        <div className="k8s-nodes" role="img" aria-label="Kubernetes 部署拓扑：六个工作节点各自运行一组 API、supervisor、worker、MCP、Postgres 与平台类 Pod，水平 Pod 自动伸缩器依据 p95 延迟伸缩 API、researcher 与 executor Pod。">
          {K8S_NODES.map((n) => (
            <div className="k8s-node" key={n.name}>
              <div className="hd">
                <b>{n.name}</b>
                <span>cpu {n.cpu} · mem {n.mem}</span>
              </div>
              <div className="pods">
                {n.pods.map((p, i) => (
                  <div key={`${p.label}-${i}`} className={`pod${p.kind ? ` ${p.kind}` : ''}`}>{p.label}</div>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="legend-row">
          <span><i style={{ background: 'oklch(0.28 0.07 240 / 0.5)', border: '1px solid var(--blue-3)' }} />api</span>
          <span><i style={{ background: 'oklch(0.42 0.13 240 / 0.4)', border: '1px solid var(--blue-3)' }} />supervisor</span>
          <span><i style={{ background: 'oklch(0.28 0.08 295 / 0.5)', border: '1px solid var(--purple-3)' }} />researcher</span>
          <span><i style={{ background: 'oklch(0.26 0.06 160 / 0.5)', border: '1px solid var(--emerald-3)' }} />analyzer</span>
          <span><i style={{ background: 'oklch(0.30 0.06 75 / 0.5)', border: '1px solid var(--amber-3)' }} />executor</span>
          <span><i style={{ background: 'oklch(0.26 0.06 160 / 0.3)', border: '1px solid var(--emerald-2)' }} />postgres</span>
          <span><i style={{ background: 'oklch(0.28 0.10 25 / 0.3)', border: '1px solid var(--red-3)' }} />重启中</span>
          <span style={{ marginLeft: 'auto' }}>HPA 依据 p95 延迟伸缩 api · researcher · executor</span>
        </div>
      </div>
    </section>
  )
}

/* ── 08 · Multi-region failover & air-gap ────────────────────────────── */
function Section08() {
  return (
    <section className="block">
      <SectionHeader num="08" title="多区域故障转移与气隙隔离" sub="检查点复制 · RPO 5s · 气隙对等" />
      <div className="canvas">
        <div className="meta-strip">
          <span>3 个区域 · 1 个气隙隔离区 · 跨区域 p99 84ms</span>
          <span>RPO 5s · RTO 90s</span>
        </div>
        <svg viewBox="0 0 1280 380" width="100%" height={380} className="diagram-grid" role="img" aria-label="多区域故障转移与气隙隔离：主区域把预写日志变更流式复制到温备的只读备用区域，而一个完全气隙隔离的隔离区则以本地 LLM 独立运行，仅通过签名的离线包接收更新。">
          <defs>
            <marker id="arrow-r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
              <path d="M0,0 L10,5 L0,10 z" fill="var(--fg-muted)" />
            </marker>
          </defs>
          <Region x={80} color="var(--blue-3)" eyebrowColor="var(--blue-4)" eyebrow="主区域 · us-east-1" title="在线工作负载" sub="6 个节点 · 128 个 Pod · 4.8k rps" rows={[
            { eyebrow: 'PG 主库', eyebrowColor: 'var(--emerald-4)', body: '检查点 · 记忆 · 审计' },
            { eyebrow: '控制平面', eyebrowColor: 'var(--blue-4)', body: 'api · supervisor · mcp · agents' },
            { eyebrow: '事件总线', eyebrowColor: 'var(--red-4)', body: 'kafka · redis · otel' },
          ]} />
          <Region x={490} color="var(--purple-3)" eyebrowColor="var(--purple-4)" eyebrow="温备 · eu-west-2" title="只读备用" sub="4 个节点 · 84 个 Pod · 2.1k rps" rows={[
            { eyebrow: 'PG 副本 · 5s 延迟', eyebrowColor: 'var(--emerald-4)', body: '只读 · 可随时提升为主库' },
            { eyebrow: '控制平面', eyebrowColor: 'var(--purple-4)', body: '读流量 · 故障转移就绪' },
            { eyebrow: '镜像总线', eyebrowColor: 'var(--red-4)', body: 'kafka mirror-maker · 80ms p99' },
          ]} />
          <g transform="translate(900 60)">
            <rect width="320" height="260" rx="14" fill="oklch(0.20 0.013 250 / 0.5)" stroke="var(--amber-2)" strokeWidth="1.5" strokeDasharray="6 4" />
            <text x="20" y="26" fontFamily="var(--font-mono)" fontSize="10" fill="var(--amber-4)" letterSpacing="2">气隙隔离 · 政务专区</text>
            <text x="20" y="44" fontFamily="var(--font-sans)" fontSize="14" fill="var(--fg-primary)" fontWeight="500">离线隔离区</text>
            <text x="20" y="60" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)">2 个节点 · 64 个 Pod · Ollama · 0 外联</text>
            <RegionRow y={84} width={280} eyebrow="PG · 本地" eyebrowColor="var(--emerald-4)" body="独立状态 · WORM 审计" />
            <RegionRow y={138} width={280} eyebrow="本地 LLM" eyebrowColor="var(--amber-4)" body="ollama · llama-3.1 · 8b + 70b" />
            <RegionRow y={192} width={280} eyebrow="更新通道" eyebrowColor="var(--fg-muted)" body="签名包 · USB · 4.2GB" />
          </g>
          <g fill="none">
            <path d="M 360 200 C 420 200, 430 200, 490 200" stroke="var(--emerald-3)" strokeWidth="2" markerEnd="url(#arrow-r)" />
            <text x="380" y="190" fontFamily="var(--font-mono)" fontSize="10" fill="var(--emerald-4)">WAL · 5s 延迟</text>
          </g>
          <g fill="none">
            <line x1="770" y1="190" x2="900" y2="190" stroke="var(--fg-faint)" strokeDasharray="2 6" strokeWidth="2" />
            <g transform="translate(820 185)">
              <circle cx="0" cy="0" r="10" fill="var(--bg-canvas)" stroke="var(--fg-muted)" strokeWidth="1" />
              <text x="0" y="3" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)">✕</text>
            </g>
            <text x="800" y="172" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)">无网络 · USB 离线包</text>
          </g>
        </svg>
      </div>
    </section>
  )
}

function Region({
  x, color, eyebrowColor, eyebrow, title, sub, rows,
}: {
  x: number; color: string; eyebrowColor: string; eyebrow: string; title: string; sub: string;
  rows: { eyebrow: string; eyebrowColor: string; body: string }[]
}) {
  return (
    <g transform={`translate(${x} 60)`}>
      <rect width="280" height="260" rx="14" fill="oklch(0.20 0.013 250 / 0.5)" stroke={color} strokeWidth="1.5" />
      <text x="20" y="26" fontFamily="var(--font-mono)" fontSize="10" fill={eyebrowColor} letterSpacing="2">{eyebrow}</text>
      <text x="20" y="44" fontFamily="var(--font-sans)" fontSize="14" fill="var(--fg-primary)" fontWeight="500">{title}</text>
      <text x="20" y="60" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)">{sub}</text>
      {rows.map((r, i) => (
        <RegionRow key={r.eyebrow} y={84 + i * 54} width={240} eyebrow={r.eyebrow} eyebrowColor={r.eyebrowColor} body={r.body} />
      ))}
    </g>
  )
}

function RegionRow({ y, width, eyebrow, eyebrowColor, body }: { y: number; width: number; eyebrow: string; eyebrowColor: string; body: string }) {
  return (
    <g transform={`translate(20 ${y})`}>
      <rect width={width} height="42" rx="6" fill="var(--bg-elevated)" stroke="var(--border-default)" />
      <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill={eyebrowColor} letterSpacing="2">{eyebrow}</text>
      <text x="14" y="34" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-primary)">{body}</text>
    </g>
  )
}

/* ── Endnote ─────────────────────────────────────────────────────────── */
function Endnote() {
  return (
    <section style={{
      marginTop: 72,
      padding: '28px 32px',
      border: '1px solid var(--border-default)',
      borderRadius: 'var(--r-4)',
      background:
        'radial-gradient(80% 70% at 100% 0%, oklch(0.72 0.18 240 / 0.10), transparent 60%),' +
        'radial-gradient(80% 70% at 0% 100%, oklch(0.70 0.20 295 / 0.10), transparent 60%),' +
        'var(--bg-canvas)',
    }}>
      <div className="section-eyebrow">实现参考</div>
      <h2 style={{ fontSize: 'var(--fs-30)', fontWeight: 500, letterSpacing: 'var(--tracking-tight)', margin: '6px 0 8px' }}>
        我们构建控制台所用的前端技术栈。
      </h2>
      <p style={{ color: 'var(--fg-secondary)', maxWidth: '70ch', fontSize: 'var(--fs-14)' }}>
        可将其作为你自建界面的起点。刻意选择「无聊」的技术 —— 控制台需要比几轮框架更替更长寿。
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 24, marginTop: 28 }}>
        <ColumnNote eyebrow="UI 运行时" body="React 19 · TypeScript · Vite · tokens.css。不使用任何 UI 组件库。" />
        <ColumnNote eyebrow="实时" body="运行流采用 SSE · 失效刷新采用 TanStack Query · 临时 UI 采用组件状态。" />
        <ColumnNote eyebrow="图形" body="拓扑图采用手写 SVG + animateMotion · 图表采用 CSS 渐变。" />
        <ColumnNote eyebrow="图表" body="看板采用内联 SVG · 高基数时间序列才使用 Recharts。" />
      </div>
    </section>
  )
}
