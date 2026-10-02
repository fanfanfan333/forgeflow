import { useEffect } from 'react'
import { Link } from '@tanstack/react-router'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import '../styles/landing.css'

const CONSOLE_HREF = '/console' as const

export function LandingPage() {
  useDocumentTitle('为生产级 AI 智能体打造的操作系统')
  // Toggle a body class so landing-only CSS scopes cleanly (no overflow-x clip on dashboard pages).
  useEffect(() => {
    document.body.classList.add('landing')
    return () => document.body.classList.remove('landing')
  }, [])

  return (
    <div className="landing-root" data-theme="dark">
      <LandingNav />
      <Hero />
      <LogoStrip />
      <Architecture />
      <Platform />
      <ObservabilityPreview />
      <Developers />
      <Enterprise />
      <Docs />
      <CallToAction />
      <Footer />
    </div>
  )
}

function LandingNav() {
  return (
    <nav className="top">
      <div className="wrap inner">
        <div className="row gap-6">
          <Link to="/" className="brand">
            <span className="brand-name">ForgeFlow</span>
          </Link>
          <ul>
            <li><a href="#platform">平台</a></li>
            <li><a href="#architecture">架构</a></li>
            <li><a href="#observability">可观测性</a></li>
            <li><a href="#enterprise">企业级</a></li>
            <li><a href="#developers">开发者</a></li>
            <li><a href="#docs">文档</a></li>
          </ul>
        </div>
        <div className="right">
          <span className="nav-pill">
            <span className="tag">新</span> 检查点运行现已支持跨 Pod 恢复
            <span className="arrow">→</span>
          </span>
          <a href={CONSOLE_HREF} className="btn ghost">
            登录
          </a>
          <a href={CONSOLE_HREF} className="btn primary">
            打开控制台
          </a>
        </div>
      </div>
    </nav>
  )
}

function Hero() {
  return (
    <section className="hero">
      <div className="spotlight" />
      <div className="wrap" style={{ position: 'relative', zIndex: 1 }}>
        <span className="release-pill">
          <span className="tag">新</span>
          使用本地 Ollama 守护进程，完整离线运行
          <span className="arrow">→</span>
        </span>
        <h1>
          为<em>生产级 AI&nbsp;智能体</em>打造的操作系统。
        </h1>
        <p className="lede">
          ForgeFlow 编排贯穿整个业务的专职智能体团队 —— 具备人工介入审批、语义记忆、亚秒级
          可观测性，以及能让安全团队真正签核的审计轨迹。
        </p>
        <div className="hero-cta">
          <a href={CONSOLE_HREF} className="btn primary">
            打开控制台
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M3 7h8m0 0L7.5 3.5M11 7l-3.5 3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </a>
          <a href="#architecture" className="btn">
            <span className="mono" style={{ color: 'var(--fg-muted)', fontSize: 11 }}>$</span>
            docker compose up forgeflow
          </a>
        </div>

        {/* Truthful project facts — no fabricated operational metrics. */}
        <div className="hero-meta">
          <div>
            <div className="k">许可证</div>
            <div className="v">Apache 2.0</div>
          </div>
          <div>
            <div className="k">运行时</div>
            <div className="v">LangGraph</div>
          </div>
          <div>
            <div className="k">部署</div>
            <div className="v">Docker · K8s</div>
          </div>
          <div>
            <div className="k">审批</div>
            <div className="v">人工介入</div>
          </div>
        </div>
      </div>

      <p className="sr-only">
        插图：ForgeFlow 控制台展示一次销售运营工作流示例运行 —— 主控将任务分派给
        研究员、分析器与执行器智能体，并在人工审批步骤处暂停。
      </p>
      <HeroStage />
    </section>
  )
}

function HeroStage() {
  return (
    <div className="hero-stage" aria-hidden="true">
      <div className="glow" />
      <div className="frame">
        <div className="miniapp">
          <div className="chrome">
            <div className="traffic">
              <span /><span /><span />
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)', marginLeft: 8 }}>
              forgeflow.app/runs/wf_8K42n
            </div>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
              <span className="status-bar">
                <span className="dot live" /> 实时 · supervisor
              </span>
            </div>
          </div>
          <aside className="side">
            <div className="label">工作区</div>
            <ul>
              <li className="active"><span className="sq" /> 销售运营</li>
              <li><span className="sq" /> 支持</li>
              <li><span className="sq" /> 财务对账</li>
            </ul>
            <div className="label">运行</div>
            <ul>
              <li className="active"><span className="sq" /> 实时时间线</li>
              <li><span className="sq" /> 智能体拓扑图</li>
              <li><span className="sq" /> 成本</li>
              <li><span className="sq" /> 记忆</li>
            </ul>
          </aside>
          <main className="main">
            <div className="ma-bar">
              <div>
                <div style={{ color: 'var(--fg-primary)', fontWeight: 500 }}>Stripe — 销售线索资质评估</div>
                <div className="run-id">运行 ID #8K42 · 人工审批待处理</div>
              </div>
              <span className="badge amber">● 待审批</span>
            </div>
            <div className="ma-stats">
              <div className="s"><div className="v">¥0.184</div><div className="k">成本</div></div>
              <div className="s"><div className="v">12.4s</div><div className="k">耗时</div></div>
              <div className="s"><div className="v">8</div><div className="k">跳数</div></div>
              <div className="s"><div className="v">9.1<span style={{ color: 'var(--fg-muted)' }}>/10</span></div><div className="k">评估</div></div>
            </div>
            <div className="tl-box">
              <TlRule color="var(--blue-4)" who="主控" left="0" width="6%" />
              <TlRule color="var(--purple-4)" who="研究员" left="5%" width="24%" tone="purple" />
              <TlRule color="var(--blue-4)" who="主控" left="28%" width="4%" />
              <TlRule color="var(--emerald-4)" who="分析器" left="32%" width="18%" tone="emerald" />
              <TlRule color="var(--blue-4)" who="主控" left="49%" width="4%" />
              <TlRule color="var(--amber-4)" who="执行器" left="52%" width="28%" tone="amber" />
              <TlRule color="var(--fg-muted)" who="人工审批" left="79%" width="14%" tone="running" />
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}

function TlRule({
  color, who, left, width, tone,
}: { color: string; who: string; left: string; width: string; tone?: 'purple' | 'emerald' | 'amber' | 'running' }) {
  return (
    <div className="tl-rule">
      <div className="who">
        <span className="dotc" style={{ background: color }} />
        {who}
      </div>
      <div className="bar-wrap">
        <div className={`bar${tone ? ` ${tone}` : ''}`} style={{ left, width }} />
      </div>
    </div>
  )
}

function LogoStrip() {
  return (
    <div className="wrap">
      <div className="logos">
        <span className="lbl">深受以下基础设施团队信赖</span>
        <div className="marks">
          {['NORTHWIND', 'Helios.ai', 'meridian', 'ATLAS', 'Quanta', 'CADENCE'].map((m) => (
            <span key={m} className="mark">
              <span className="glyph" /> {m}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

function Architecture() {
  return (
    <section id="architecture">
      <div className="wrap">
        <div className="section-eyebrow">架构 · 中心辐射式主控</div>
        <h2>一个主控，一组专家智能体，每一步都可回放。</h2>
        <p className="sub">
          确定性的主控通过结构化输出把工作分派给专家智能体。执行体之间通过 A2A 通信，
          经由 MCP 发现工具，并把每个节点持久化到 Postgres，因此任意执行体都能恢复任意运行。
        </p>

        <div className="arch" style={{ marginTop: 48 }}>
          <ArchitectureSvg />
        </div>

        <div className="arch-callouts">
          <ArchCallout num="01 · 路由" body="主控在每一步都输出结构化的路由决策 —— 确定性、可回放、可审计。" />
          <ArchCallout num="02 · 工具" body="执行体通过 MCP 发现工具。无需改动智能体代码，即可替换提供方 —— Tavily、Salesforce 或内部实现。" />
          <ArchCallout num="03 · 通信" body="A2A 协议 —— JSON-RPC 2.0 + 能力发现。执行体彼此发现并协作，无需经主控中转扩散。" />
          <ArchCallout num="04 · 持久化" body="每个节点退出前都持久化到 Postgres。可在任意执行体上恢复任意运行 —— 跨容器组、跨区域、跨重启。" />
        </div>
      </div>
    </section>
  )
}

function ArchCallout({ num, body }: { num: string; body: React.ReactNode }) {
  return (
    <div>
      <div className="section-eyebrow">{num}</div>
      <p style={{ marginTop: 8, color: 'var(--fg-secondary)', fontSize: 'var(--fs-14)' }}>{body}</p>
    </div>
  )
}

function ArchitectureSvg() {
  return (
    <svg viewBox="0 0 1200 540" width="100%" height={540} style={{ display: 'block', position: 'relative', zIndex: 1 }}>
      <defs>
        <linearGradient id="edge-blue" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="oklch(0.72 0.18 240)" stopOpacity="0.05" />
          <stop offset="50%" stopColor="oklch(0.72 0.18 240)" stopOpacity="0.55" />
          <stop offset="100%" stopColor="oklch(0.72 0.18 240)" stopOpacity="0.05" />
        </linearGradient>
        <linearGradient id="edge-purple" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="oklch(0.70 0.20 295)" stopOpacity="0.05" />
          <stop offset="50%" stopColor="oklch(0.70 0.20 295)" stopOpacity="0.55" />
          <stop offset="100%" stopColor="oklch(0.70 0.20 295)" stopOpacity="0.05" />
        </linearGradient>
        <radialGradient id="node-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="oklch(0.72 0.18 240)" stopOpacity="0.5" />
          <stop offset="100%" stopColor="oklch(0.72 0.18 240)" stopOpacity="0" />
        </radialGradient>
        <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" fill="oklch(0.30 0.012 250 / 0.5)" />
        </pattern>
      </defs>

      <rect width="1200" height="540" fill="url(#dots)" />

      <g opacity="0.6">
        <line x1="0" y1="60" x2="1200" y2="60" stroke="var(--border-subtle)" strokeDasharray="2 6" />
        <line x1="0" y1="180" x2="1200" y2="180" stroke="var(--border-subtle)" strokeDasharray="2 6" />
        <line x1="0" y1="320" x2="1200" y2="320" stroke="var(--border-subtle)" strokeDasharray="2 6" />
        <line x1="0" y1="460" x2="1200" y2="460" stroke="var(--border-subtle)" strokeDasharray="2 6" />
      </g>
      <g fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)" letterSpacing="2">
        <text x="20" y="56">客户端</text>
        <text x="20" y="176">控制平面</text>
        <text x="20" y="316">智能体</text>
        <text x="20" y="456">数据 · 记忆</text>
      </g>

      <g fill="none" strokeWidth="1.4">
        <path d="M 200 40 C 200 100, 280 140, 280 180" stroke="url(#edge-blue)" />
        <path d="M 320 220 C 460 220, 540 260, 600 280" stroke="url(#edge-blue)" />
        <path d="M 640 280 C 700 320, 820 320, 870 320" stroke="url(#edge-purple)" />
        <path d="M 640 290 C 720 360, 820 380, 870 390" stroke="url(#edge-purple)" />
        <path d="M 640 300 C 720 420, 830 450, 870 460" stroke="url(#edge-purple)" />
        <path d="M 990 320 C 1050 320, 1080 270, 1100 230" stroke="oklch(0.42 0.13 240 / 0.5)" />
        <path d="M 990 390 C 1050 390, 1080 380, 1100 360" stroke="oklch(0.42 0.13 240 / 0.5)" />
        <path d="M 920 430 C 920 470, 700 470, 540 470" stroke="oklch(0.40 0.11 160 / 0.5)" />
        <path d="M 620 320 C 620 400, 520 440, 500 460" stroke="oklch(0.40 0.11 160 / 0.5)" />
        <path d="M 930 350 C 980 360, 980 380, 930 400" stroke="oklch(0.50 0.12 75 / 0.55)" strokeDasharray="3 3" />
      </g>

      <circle r="3" fill="oklch(0.86 0.10 240)">
        <animateMotion dur="3s" repeatCount="indefinite" path="M 200 40 C 200 100, 280 140, 280 180" />
      </circle>
      <circle r="3" fill="oklch(0.86 0.10 240)">
        <animateMotion dur="3s" repeatCount="indefinite" begin="0.6s" path="M 320 220 C 460 220, 540 260, 600 280" />
      </circle>
      <circle r="3" fill="oklch(0.84 0.14 295)">
        <animateMotion dur="2.4s" repeatCount="indefinite" begin="1.2s" path="M 640 290 C 720 360, 820 380, 870 390" />
      </circle>
      <circle r="3" fill="oklch(0.84 0.14 295)">
        <animateMotion dur="2.6s" repeatCount="indefinite" begin="1.6s" path="M 640 280 C 700 320, 820 320, 870 320" />
      </circle>

      <g transform="translate(140 20)">
        <rect width="120" height="40" rx="8" fill="var(--bg-elevated)" stroke="var(--border-default)" />
        <text x="14" y="17" fontFamily="var(--font-mono)" fontSize="10" fill="var(--fg-muted)" letterSpacing="1">客户端</text>
        <text x="14" y="32" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)">控制台 · SDK · API</text>
      </g>

      <g transform="translate(220 160)">
        <rect width="120" height="60" rx="8" fill="var(--bg-elevated)" stroke="var(--border-default)" />
        <circle cx="14" cy="14" r="4" fill="var(--blue-4)" />
        <text x="24" y="18" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">FastAPI</text>
        <text x="14" y="36" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">REST + SSE</text>
        <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">:8000</text>
      </g>

      <g transform="translate(540 240)">
        <ellipse cx="50" cy="50" rx="120" ry="120" fill="url(#node-glow)" />
        <rect width="100" height="80" rx="14" fill="var(--bg-elevated)" stroke="var(--blue-3)" strokeWidth="1.5" />
        <circle cx="16" cy="18" r="5" fill="var(--blue-4)" />
        <text x="28" y="22" fontFamily="var(--font-sans)" fontSize="13" fill="var(--fg-primary)" fontWeight="600">主控</text>
        <text x="16" y="42" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">LangGraph</text>
        <text x="16" y="56" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">结构化输出</text>
        <text x="16" y="70" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)">路由 · gpt-4o</text>
      </g>

      <g transform="translate(860 290)">
        <rect width="140" height="60" rx="10" fill="var(--bg-elevated)" stroke="var(--purple-3)" strokeWidth="1.2" />
        <circle cx="14" cy="14" r="4" fill="var(--purple-4)" />
        <text x="26" y="18" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">研究员</text>
        <text x="14" y="36" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">网页检索 · 抓取</text>
        <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--purple-4)">A2A · MCP</text>
      </g>
      <g transform="translate(860 360)">
        <rect width="140" height="60" rx="10" fill="var(--bg-elevated)" stroke="var(--emerald-3)" strokeWidth="1.2" />
        <circle cx="14" cy="14" r="4" fill="var(--emerald-4)" />
        <text x="26" y="18" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">分析器</text>
        <text x="14" y="36" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">评分 · 理想客户画像 · 风险</text>
        <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--emerald-4)">结构化 0–10</text>
      </g>
      <g transform="translate(860 430)">
        <rect width="140" height="60" rx="10" fill="var(--bg-elevated)" stroke="var(--amber-2)" strokeWidth="1.2" />
        <circle cx="14" cy="14" r="4" fill="var(--amber-4)" />
        <text x="26" y="18" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">执行器</text>
        <text x="14" y="36" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">客户关系管理 · 邮件 · 写入</text>
        <text x="14" y="50" fontFamily="var(--font-mono)" fontSize="9" fill="var(--amber-4)">已人工审批</text>
      </g>

      <g transform="translate(1080 200)">
        <rect width="100" height="50" rx="8" fill="var(--bg-elevated)" stroke="var(--border-default)" />
        <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">MCP</text>
        <text x="14" y="34" fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">工具服务</text>
        <text x="14" y="46" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">:8001 · 14 个工具</text>
      </g>
      <g transform="translate(1080 330)">
        <rect width="100" height="50" rx="8" fill="var(--bg-elevated)" stroke="var(--border-default)" />
        <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill="var(--blue-4)" letterSpacing="2">A2A</text>
        <text x="14" y="34" fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">注册中心</text>
        <text x="14" y="46" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">JSON-RPC 2.0</text>
      </g>

      <g transform="translate(420 440)">
        <rect width="160" height="60" rx="10" fill="var(--bg-elevated)" stroke="var(--emerald-2)" strokeWidth="1.2" />
        <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill="var(--emerald-4)" letterSpacing="2">检查点器</text>
        <text x="14" y="36" fontFamily="var(--font-sans)" fontSize="12" fill="var(--fg-primary)" fontWeight="500">Postgres 16 + pgvector</text>
        <text x="14" y="51" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">状态 · 记忆 · 审计 · ivfflat</text>
      </g>

      <g transform="translate(700 450)">
        <rect width="120" height="50" rx="8" fill="var(--bg-elevated)" stroke="var(--border-default)" />
        <text x="14" y="18" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)" letterSpacing="2">事件</text>
        <text x="14" y="34" fontFamily="var(--font-sans)" fontSize="11" fill="var(--fg-primary)">Kafka + Redis</text>
        <text x="14" y="46" fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)">流 · 审计</text>
      </g>
    </svg>
  )
}

function Platform() {
  return (
    <section id="platform">
      <div className="wrap">
        <div className="section-eyebrow">平台</div>
        <h2>AI 平台团队需要构建的一切，这里都已备好。</h2>
        <p className="sub">
          路由、记忆、工具、评估、治理、可观测性。开箱即用的生产级范式 —— 而非笔记本里的演示。
        </p>

        <div className="features">
          <Feature
            color="var(--blue-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="7" cy="7" r="3" stroke="currentColor" strokeWidth="1.4" />
                <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.4" strokeDasharray="2 2" />
              </svg>
            }
            title="多智能体编排"
            body="主控 + 执行体模式，具备确定性、结构化的路由。像部署普通服务一样，轻松启动并上线数十个专家智能体。"
          />
          <Feature
            color="var(--purple-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M3 7h8M7 3v8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            }
            title="人工介入审批"
            body="可在任意节点前中断。审批人可查看拟执行动作、完整上下文与成本 —— 然后批准、驳回，或附注退回。"
          />
          <Feature
            color="var(--emerald-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <rect x="2" y="2" width="4" height="4" rx="1" stroke="currentColor" strokeWidth="1.4" />
                <rect x="8" y="2" width="4" height="4" rx="1" stroke="currentColor" strokeWidth="1.4" />
                <rect x="2" y="8" width="4" height="4" rx="1" stroke="currentColor" strokeWidth="1.4" />
                <rect x="8" y="8" width="4" height="4" rx="1" stroke="currentColor" strokeWidth="1.4" />
              </svg>
            }
            title="语义记忆"
            body="Postgres + pgvector，按命名空间隔离。用余弦检索召回任意历史决策 —— 与事务状态共置一处。"
          />
          <Feature
            color="var(--amber-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M2 11l3-3 2 2 5-5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M9 3h3v3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            }
            title="成本与评估流水线"
            body="按 Token、按智能体、按租户统计成本。以 LLM-as-judge 对忠实度、相关性、幻觉进行评分 —— 每次运行自动执行。"
          />
          <Feature
            color="var(--blue-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M1 7h2l2-4 4 8 2-4h2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            }
            title="亚秒级可观测性"
            body="流式呈现每个节点、每次工具调用、每个 Token。包含追踪时间线、智能体拓扑图、语义事件检索与 OTel 导出。"
          />
          <Feature
            color="var(--red-4)"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <rect x="3" y="6" width="8" height="6" rx="1" stroke="currentColor" strokeWidth="1.4" />
                <path d="M5 6V4a2 2 0 0 1 4 0v2" stroke="currentColor" strokeWidth="1.4" />
              </svg>
            }
            title="企业级 RBAC + 审计"
            body="基于角色的访问控制、限定范围的 API Token，以及不可篡改的审计日志。支持 OIDC SSO 与多因素认证。可借助 Ollama 进行气隙隔离部署。"
          />
        </div>
      </div>
    </section>
  )
}

function Feature({ color, icon, title, body }: { color: string; icon: React.ReactNode; title: string; body: string }) {
  return (
    <div className="feature">
      <div className="icon" style={{ color }}>{icon}</div>
      <h3>{title}</h3>
      <p>{body}</p>
    </div>
  )
}

function ObservabilityPreview() {
  return (
    <section id="observability">
      <div className="wrap">
        <div className="section-eyebrow">可观测性</div>
        <h2>像运维技术栈其他部分一样运维 AI。</h2>
        <p className="sub">
          实时的智能体时间线、成本消耗、故障根因分析、评估得分 —— 一个 SRE 所熟悉的控制台，
          并针对非确定性系统做了调优。
        </p>

        <div className="preview-frame">
          <div className="preview-chrome">
            <div className="traffic"><span /><span /><span /></div>
            <span className="mono" style={{ color: 'var(--fg-muted)', fontSize: 11, marginLeft: 12 }}>
              app.forgeflow.io · 控制台
            </span>
            <span className="status-bar" style={{ marginLeft: 'auto' }}>
              <span className="dot live" /> 12 次运行流式传输中
            </span>
            <a href={CONSOLE_HREF} className="btn sm" style={{ marginLeft: 12 }}>
              打开实时控制台 →
            </a>
          </div>
          <div className="preview-split">
            <aside style={{ borderRight: '1px solid var(--border-subtle)', padding: '18px 14px', background: 'var(--bg-page)' }}>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-muted)', letterSpacing: '.12em', textTransform: 'uppercase', margin: '6px 0 10px' }}>
                工作区
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', background: 'var(--bg-elevated)', borderRadius: 6, border: '1px solid var(--border-subtle)' }}>
                <div style={{ width: 18, height: 18, borderRadius: 4, background: 'linear-gradient(135deg, var(--blue-4), var(--purple-4))' }} />
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  <span style={{ fontSize: 12.5, fontWeight: 500 }}>示例工作区 · 销售运营</span>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--fg-muted)' }}>主生产环境</span>
                </div>
              </div>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-muted)', letterSpacing: '.12em', textTransform: 'uppercase', margin: '24px 0 8px' }}>
                运行视图
              </div>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12.5 }}>
                <li style={{ padding: '5px 8px', borderRadius: 5, background: 'var(--bg-elevated)', color: 'var(--fg-primary)', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--blue-4)' }} /> 实时时间线
                </li>
                <li style={{ padding: '5px 8px', color: 'var(--fg-secondary)' }}>智能体通信拓扑</li>
                <li style={{ padding: '5px 8px', color: 'var(--fg-secondary)' }}>工具调用追踪</li>
                <li style={{ padding: '5px 8px', color: 'var(--fg-secondary)' }}>记忆召回</li>
                <li style={{ padding: '5px 8px', color: 'var(--fg-secondary)' }}>成本明细</li>
                <li style={{ padding: '5px 8px', color: 'var(--fg-secondary)' }}>评估得分</li>
              </ul>
            </aside>
            <main style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div>
                  <div style={{ fontSize: 17, fontWeight: 500, letterSpacing: 'var(--tracking-tight)', display: 'flex', alignItems: 'center', gap: 10 }}>
                    Stripe E 轮扩张
                    <span className="badge amber">● 待审批</span>
                  </div>
                  <div className="mono" style={{ fontSize: 11.5, color: 'var(--fg-muted)', marginTop: 4 }}>
                    运行 ID #8K42 · 销售线索资质评估 · 于 12.4s 前启动
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button className="btn sm">重放</button>
                  <button className="btn sm primary">批准</button>
                </div>
              </div>

              <div className="preview-kpis">
                <PreviewKpi label="成本" value="¥0.184" />
                <PreviewKpi label="Token" value="14,892" />
                <PreviewKpi label="耗时" value="12.4s" />
                <PreviewKpi label="跳数" value="8" />
                <PreviewKpi label="评估得分（评审模型）" value={<>9.1<span style={{ color: 'var(--fg-muted)', fontSize: 13 }}>/10</span></>} valueColor="var(--emerald-4)" />
              </div>

              <div style={{ border: '1px solid var(--border-subtle)', borderRadius: 8, padding: '18px 20px', background: 'var(--bg-page)', flex: 1 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 14 }}>
                  <div style={{ fontSize: 13, color: 'var(--fg-secondary)', fontWeight: 500 }}>执行时间线</div>
                  <div className="mono" style={{ fontSize: 10, color: 'var(--fg-muted)' }}>甘特图 · 12.4s 窗口</div>
                </div>
                <div style={{ display: 'grid', gridTemplateRows: 'repeat(7, 24px)', gap: 6 }}>
                  <PreviewGantt who="主控" left="0" width="5%" />
                  <PreviewGantt who="研究员" left="5%" width="28%" tone="purple" />
                  <PreviewGantt who="主控" left="33%" width="4%" />
                  <PreviewGantt who="分析器" left="37%" width="18%" tone="emerald" />
                  <PreviewGantt who="主控" left="55%" width="4%" />
                  <PreviewGantt who="执行器" left="59%" width="23%" tone="amber" />
                  <PreviewGantt who="人工审批" left="82%" width="14%" tone="running" />
                </div>
              </div>
            </main>
          </div>
        </div>
      </div>
    </section>
  )
}

function PreviewKpi({ label, value, valueColor }: { label: string; value: React.ReactNode; valueColor?: string }) {
  return (
    <div style={{ background: 'var(--bg-canvas)', padding: '12px 14px' }}>
      <div style={{ fontSize: 10, color: 'var(--fg-muted)', fontFamily: 'var(--font-mono)', letterSpacing: '.1em', textTransform: 'uppercase' }}>{label}</div>
      <div className="mono" style={{ fontSize: 20, color: valueColor ?? 'var(--fg-primary)', marginTop: 4 }}>{value}</div>
    </div>
  )
}

function PreviewGantt({ who, left, width, tone }: { who: string; left: string; width: string; tone?: 'purple' | 'emerald' | 'amber' | 'running' }) {
  const bgMap: Record<string, string> = {
    purple: 'linear-gradient(90deg, var(--purple-3), var(--purple-4))',
    emerald: 'linear-gradient(90deg, var(--emerald-3), var(--emerald-4))',
    amber: 'linear-gradient(90deg, var(--amber-2), var(--amber-4))',
    running: 'linear-gradient(90deg, var(--blue-3), var(--blue-4))',
  }
  return (
    <div className="gantt-row">
      <div className="mono" style={{ fontSize: 11, color: 'var(--fg-secondary)' }}>▷ {who}</div>
      <div style={{ position: 'relative', height: 14, background: 'var(--bg-inset)', borderRadius: 4 }}>
        <div
          style={{
            position: 'absolute',
            left,
            width,
            top: 0,
            bottom: 0,
            background: tone ? bgMap[tone] : 'linear-gradient(90deg, var(--blue-3), var(--blue-4))',
            borderRadius: 4,
            animation: tone === 'running' ? 'stream 1.6s var(--ease-out) infinite alternate' : undefined,
          }}
        />
      </div>
    </div>
  )
}

function Developers() {
  const code = `# pip install forgeflow
from forgeflow import Workflow, Supervisor, Agent
from forgeflow.tools import mcp

researcher = Agent("researcher", tools=mcp("http://mcp:8001"))
analyzer   = Agent("analyzer",   structured=LeadScore)
executor   = Agent("executor",   approval=True)

wf = Workflow(
    supervisor=Supervisor(model="gpt-4o"),
    workers=[researcher, analyzer, executor],
    checkpoint="postgres://forgeflow",
)

async for event in wf.stream({"company": "Stripe"}):
    print(event.agent, event.cost, event.tokens)
#  supervisor   ¥0.0008   72
#  researcher   ¥0.0241   2,431
#  analyzer     ¥0.0094   941
#  ...`

  return (
    <section id="developers">
      <div className="wrap">
        <div className="dev-split">
          <div>
            <div className="section-eyebrow">开发者</div>
            <h2>从 import 到流式生产运行，仅需五行。</h2>
            <p className="sub">
              强类型的 Python SDK、以声明式为主的图构建器，以及前端团队真正能据此生成客户端的
              OpenAPI 接口。
            </p>
            <div className="hero-cta" style={{ marginTop: 28 }}>
              <a href="/docs" className="btn primary">阅读文档 →</a>
              <a href="/api/docs" target="_blank" rel="noopener noreferrer" className="btn">API 参考</a>
            </div>

            <div className="dx-features">
              <DxFeature title="本地优先的开发闭环" body="compose 中内置 Ollama + Postgres，无需提交任何密钥。" />
              <DxFeature title="可恢复的运行" body="每个节点都会持久化一个 Postgres 检查点；可从任意检查点恢复。" />
              <DxFeature title="全链路强类型" body="端到端使用 Pydantic 状态 + 结构化输出。" />
              <DxFeature title="原生 OTel" body="Span、指标、日志，直通你的技术栈。" />
            </div>
          </div>

          <pre className="code">{code}</pre>
        </div>
      </div>
    </section>
  )
}

function DxFeature({ title, body }: { title: string; body: string }) {
  return (
    <div>
      <h4 style={{ fontSize: 13, fontWeight: 500, margin: '0 0 4px', color: 'var(--fg-primary)' }}>{title}</h4>
      <p style={{ margin: 0, color: 'var(--fg-muted)', fontSize: 12.5 }}>{body}</p>
    </div>
  )
}

function Enterprise() {
  return (
    <section id="enterprise">
      <div className="wrap">
        <div className="section-eyebrow">企业级</div>
        <h2>为安全审查而构建。</h2>
        <p className="sub">
          OIDC SSO、TOTP MFA、Argon2id 口令哈希、基于角色的访问控制，以及不可篡改的审计日志。
          可在你自己的 VPC、自己的 Kubernetes 上运行，或借助本地 Ollama 守护进程完全气隙隔离运行。
        </p>

        <div className="trust-grid">
          <div style={{ position: 'relative', aspectRatio: '1', borderRadius: 'var(--r-5)', border: '1px solid var(--border-default)', background: 'var(--bg-canvas)', display: 'grid', placeItems: 'center', overflow: 'hidden' }}>
            <div className="spotlight" />
            <svg
              viewBox="0 0 200 200"
              width={180}
              height={180}
              style={{ position: 'relative', zIndex: 1 }}
              role="img"
              aria-label="安全控制：基于角色的访问控制、不可篡改的审计日志、OIDC 单点登录，以及多因素认证。"
            >
              <g fill="none" stroke="var(--border-strong)" strokeWidth="0.6">
                <circle cx="100" cy="100" r="40" />
                <circle cx="100" cy="100" r="58" />
                <circle cx="100" cy="100" r="76" />
                <circle cx="100" cy="100" r="94" />
              </g>
              <g fontFamily="var(--font-mono)" fontSize="9" fill="var(--fg-muted)" letterSpacing="2">
                <text x="16" y="105">RBAC</text>
                <text x="146" y="105">审计</text>
                <text x="82" y="14">OIDC</text>
                <text x="84" y="198">多因素</text>
              </g>
              <polygon points="100,55 130,75 130,115 100,140 70,115 70,75" fill="oklch(0.20 0.013 250)" stroke="var(--blue-3)" strokeWidth="1.4" />
              <path d="M85 100 l10 10 l22 -22" stroke="var(--blue-4)" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>

          <div className="trust-list">
            <TrustItem title="身份与访问" body="OIDC 单点登录、TOTP 多因素认证、Argon2id 口令哈希，以及配合限定范围 API Token 的基于角色的访问控制。" badges={['OIDC', 'TOTP MFA', 'Argon2id']} />
            <TrustItem title="策略与治理" body="在每个 API 请求上强制执行基于角色的访问控制，配合限定范围的 Bearer Token 与按命名空间的数据隔离。" badges={['RBAC', '限定范围 Token', '命名空间隔离']} />
            <TrustItem title="审计与留存" body="不可篡改的仅追加审计日志，按时间分区，提供只读检索与导出。资源与预算的删除按租户隔离：越权删除一律拒绝，删除后不再出现在任何列表或详情中。" badges={['不可篡改日志', '仅追加', '按租户隔离删除']} />
            <TrustItem title="气隙隔离部署" body="针对本地 Ollama 守护进程端到端运行 —— 无密钥、无外联、无第三方服务。" badges={['Ollama', '自托管', '无外联']} />
          </div>
        </div>
      </div>
    </section>
  )
}

function TrustItem({ title, body, badges }: { title: string; body: string; badges: string[] }) {
  return (
    <div className="trust-item">
      <h4>{title}</h4>
      <p>{body}</p>
      <div className="badge-row">
        {badges.map((b) => (
          <span key={b} className="badge">{b}</span>
        ))}
      </div>
    </div>
  )
}

function Docs() {
  return (
    <section id="docs">
      <div className="wrap">
        <div className="section-eyebrow">文档 · v0.1.0 · 预发布</div>
        <h2>交付、运维与扩展 ForgeFlow 所需的一切。</h2>
        <p className="sub">
          三条命令即可快速上手，提供完整的交互式接口文档，
          以及值班团队真正在用的生产运维手册。
        </p>

        <DocSearch />
        <QuickstartAndChangelog />
        <BrowseBySurface />
        <DeveloperReference />
      </div>
    </section>
  )
}

function DocSearch() {
  return (
    <a
      href="/api/docs"
      target="_blank"
      rel="noopener noreferrer"
      aria-label="在新标签页中打开交互式 REST API 参考"
      style={{
        marginTop: 40,
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '12px 18px',
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-default)',
        borderRadius: 'var(--r-3)',
        boxShadow: 'var(--shadow-sm)',
        maxWidth: 720,
        textDecoration: 'none',
      }}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ color: 'var(--fg-muted)', flexShrink: 0 }} aria-hidden="true">
        <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.4" />
        <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      <span style={{ flex: 1, color: 'var(--fg-secondary)', fontSize: 14 }}>
        浏览交互式 REST API 参考
      </span>
      <span className="badge mono" style={{ fontSize: 10 }}>接口文档</span>
      <span aria-hidden="true" style={{ color: 'var(--blue-4)' }}>→</span>
    </a>
  )
}

function QuickstartAndChangelog() {
  return (
    <div className="docs-split">
      <Quickstart />
      <Changelog />
    </div>
  )
}

function Quickstart() {
  const code = `# 1. clone + configure
$ git clone https://github.com/JoelJohnsonThomas/forgeflow
$ cp .env.example .env  # set API_SECRET_KEY (LLM: local Ollama)

# 2. boot the stack
$ docker compose --profile migration run --rm migrate
$ docker compose up -d

# 3. get a dev token, then trigger your first workflow
$ TOKEN=$(curl -s http://localhost:8000/auth/login \\
    -H "Content-Type: application/json" \\
    -d '{"user_id":"rep-1","password":"change-me-locally-only"}' | jq -r .access_token)
$ curl -X POST http://localhost:8000/workflows/run \\
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
    -d '{"workflow_type":"sales_ops","lead_data":{"company_name":"Stripe"}}'`

  return (
    <div
      style={{
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--r-4)',
        padding: 24,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            letterSpacing: '.14em',
            textTransform: 'uppercase',
            color: 'var(--blue-4)',
          }}
        >
          快速上手 · 5 分钟
        </span>
        <span className="badge mono" style={{ fontSize: 10 }}>
          自托管 · Apache 2.0
        </span>
      </div>
      <h3 style={{ margin: '0 0 14px', fontSize: 22, fontWeight: 500, letterSpacing: 'var(--tracking-tight)' }}>
        三条命令，从克隆代码仓库直达流式工作流。
      </h3>
      <pre className="code" style={{ margin: 0, fontSize: 12.5 }}>{code}</pre>
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <a href="/docs/tutorials-first-workflow" className="btn primary sm">
          打开快速上手 →
        </a>
        <a href="/api/docs" target="_blank" rel="noopener noreferrer" className="btn sm">
          REST 参考
        </a>
        <a href="https://github.com/JoelJohnsonThomas/forgeflow" target="_blank" rel="noopener noreferrer" className="btn ghost sm">
          GitHub ↗
        </a>
      </div>
    </div>
  )
}

type ChangelogEntry = {
  version: string
  date: string
  tag: 'release' | 'security' | 'fix'
  title: string
}

// Mirrors the repo CHANGELOG.md (Keep a Changelog). The project is pre-1.0;
// the top entries are unreleased work on `main`, then the 0.1.0 release.
const CHANGELOG: ChangelogEntry[] = [
  { version: '未发布', date: 'main', tag: 'security', title: '企业级认证 —— Argon2id 口令哈希、TOTP 多因素认证、轮换式刷新令牌、OIDC 交换' },
  { version: '未发布', date: 'main', tag: 'security', title: '工作流详情与轨迹接口上的对象级授权（修复越权访问）' },
  { version: '未发布', date: 'main', tag: 'fix', title: '工作流运行接口的执行超时改为返回 504，而不再占用执行体' },
  { version: '未发布', date: 'main', tag: 'release', title: 'React 19 控制台视图按需分包；初始包体积 约 547 kB → 约 364 kB' },
  { version: 'v0.1.0', date: '2026', tag: 'release', title: '首次公开发布 —— 主控多智能体内核、MCP、A2A、pgvector 记忆' },
]

function tagBadge(tag: ChangelogEntry['tag']) {
  if (tag === 'release') return <span className="badge blue" style={{ fontSize: 9 }}>发布</span>
  if (tag === 'security') return <span className="badge red" style={{ fontSize: 9 }}>安全</span>
  return <span className="badge amber" style={{ fontSize: 9 }}>修复</span>
}

function Changelog() {
  return (
    <div
      style={{
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--r-4)',
        padding: 24,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '.14em',
          textTransform: 'uppercase',
          color: 'var(--fg-muted)',
          marginBottom: 14,
        }}
      >
        最新动态 · 变更日志
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {CHANGELOG.map((c, i) => (
          <div
            key={c.title}
            className="changelog-row"
            style={{ borderTop: i === 0 ? 'none' : '1px dashed var(--border-subtle)' }}
          >
            <span className="mono" style={{ fontSize: 11, color: 'var(--blue-4)' }}>{c.version}</span>
            <span style={{ fontSize: 12.5, color: 'var(--fg-secondary)', lineHeight: 1.5 }}>{c.title}</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {tagBadge(c.tag)}
              <span className="mono" style={{ fontSize: 10, color: 'var(--fg-muted)' }}>{c.date}</span>
            </span>
          </div>
        ))}
      </div>
      <a
        href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/CHANGELOG.md"
        target="_blank"
        rel="noopener noreferrer"
        style={{ marginTop: 'auto', paddingTop: 14, fontSize: 12, color: 'var(--blue-4)', textDecoration: 'none' }}
      >
        在 GitHub 查看完整变更日志 →
      </a>
    </div>
  )
}

type Surface = {
  num: string
  name: string
  count: number
  icon: React.ReactNode
  href: string
  articles: string[]
}

const SURFACES: Surface[] = [
  {
    num: '01',
    name: '快速上手与概念',
    count: 12,
    icon: <PlayIcon />,
    href: '/docs/tutorials',
    articles: [
      '使用 Docker 五分钟安装',
      '你的第一个工作流：销售线索资质评估',
      '心智模型：主控 + 执行体',
      '状态存于何处：检查点机制',
    ],
  },
  {
    num: '02',
    name: 'REST API',
    count: 38,
    icon: <ApiIcon />,
    href: '/api/docs',
    articles: [
      'POST /workflows/run —— 触发一次运行',
      'POST /workflows/stream —— SSE 时间线',
      'GET /approvals/pending —— 待审队列',
      'POST /auth/login —— 签发 JWT',
    ],
  },
  {
    num: '03',
    name: 'Python 包',
    count: 24,
    icon: <CodeIcon />,
    href: '/api/docs',
    articles: [
      '导入 forgeflow 包',
      '工作流 + 主控 + 智能体类',
      '使用 Pydantic 的强类型状态',
      '基于服务器发送事件（SSE）的异步流式传输',
    ],
  },
  {
    num: '04',
    name: '运维手册',
    count: 41,
    icon: <BookIcon />,
    href: '/docs/operations-backup-dr',
    articles: [
      '值班手册：卡住的工作流',
      '容量规划：从 RPS 到 Pod 数量',
      '成本护栏：BudgetGuard 调优',
      '事故后重放检查点',
    ],
  },
  {
    num: '05',
    name: '安全与访问',
    count: 19,
    icon: <ShieldIcon />,
    href: '/docs/auth',
    articles: [
      'OIDC 单点登录配置',
      'TOTP 多因素认证',
      'RBAC 角色与限定范围的 API Token',
      '审计日志留存与命名空间隔离',
    ],
  },
  {
    num: '06',
    name: '设计系统',
    count: 60,
    icon: <PaintIcon />,
    href: '/design-system',
    articles: [
      '设计令牌 · oklch 色彩、字体、间距',
      '组件画廊',
      '动效原则',
      '智能体头像 + 工作流节点',
    ],
  },
]

function BrowseBySurface() {
  return (
    <div style={{ marginTop: 56 }}>
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          letterSpacing: '.14em',
          textTransform: 'uppercase',
          color: 'var(--fg-muted)',
          marginBottom: 18,
        }}
      >
        按版块浏览
      </div>
      <div className="surface-grid">
        {SURFACES.map((s) => (
          <SurfaceCard key={s.num} surface={s} />
        ))}
      </div>
    </div>
  )
}

function SurfaceCard({ surface }: { surface: Surface }) {
  return (
    <div
      style={{
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--r-4)',
        padding: 22,
        display: 'flex',
        flexDirection: 'column',
        transition: 'border-color 180ms',
      }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--border-default)' }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border-subtle)' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div
          style={{
            width: 32, height: 32, borderRadius: 8,
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border-default)',
            display: 'grid', placeItems: 'center',
            color: 'var(--blue-4)',
          }}
        >
          {surface.icon}
        </div>
        <span className="mono" style={{ fontSize: 10, color: 'var(--fg-muted)', letterSpacing: '.12em' }}>
          {surface.num} · 参考
        </span>
      </div>
      <h4
        style={{
          margin: '0 0 12px',
          fontSize: 15,
          fontWeight: 500,
          letterSpacing: 'var(--tracking-tight)',
          color: 'var(--fg-primary)',
        }}
      >
        {surface.name}
      </h4>
      <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 14px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {surface.articles.map((a) => (
          <li
            key={a}
            style={{
              fontSize: 12.5,
              color: 'var(--fg-secondary)',
              paddingLeft: 14,
              position: 'relative',
              lineHeight: 1.5,
            }}
          >
            <span
              style={{
                position: 'absolute',
                left: 0,
                top: 9,
                width: 6,
                height: 1,
                background: 'var(--fg-faint)',
              }}
            />
            {a}
          </li>
        ))}
      </ul>
      <a
        href={surface.href}
        target={surface.href.startsWith('/api') ? '_blank' : undefined}
        rel={surface.href.startsWith('/api') ? 'noopener noreferrer' : undefined}
        style={{ marginTop: 'auto', fontSize: 12, color: 'var(--blue-4)', textDecoration: 'none', fontWeight: 500 }}
      >
        打开参考 →
      </a>
    </div>
  )
}

type RefItem = {
  label: string
  sublabel: string
  href: string
  external?: boolean
}

const REFERENCES: RefItem[] = [
  { label: 'Python 包', sublabel: '通过包管理器安装', href: '/api/docs', external: true },
  { label: 'REST API', sublabel: '交互式接口文档', href: '/api/docs', external: true },
  { label: 'TypeScript SDK', sublabel: '规划中', href: '#' },
  { label: 'OpenAPI', sublabel: '接口描述文件', href: '/api/openapi.json', external: true },
  { label: '健康检查', sublabel: '健康检查探测路径', href: '/api/health', external: true },
  { label: '架构', sublabel: '参考图示', href: '/architecture' },
]

function DeveloperReference() {
  return (
    <div
      style={{
        marginTop: 56,
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--r-4)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          padding: '14px 24px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: 'var(--bg-page)',
        }}
      >
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            letterSpacing: '.14em',
            textTransform: 'uppercase',
            color: 'var(--fg-muted)',
          }}
        >
          开发者参考
        </span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)' }}>
          v0.1.0 · OpenAPI 3.1 · Apache 2.0
        </span>
      </div>
      <div className="ref-grid">
        {REFERENCES.map((r, i) => (
          <a
            key={r.label}
            href={r.href}
            target={r.external ? '_blank' : undefined}
            rel={r.external ? 'noopener noreferrer' : undefined}
            style={{
              padding: 18,
              borderLeft: i === 0 ? 'none' : '1px solid var(--border-subtle)',
              textDecoration: 'none',
              color: 'inherit',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              transition: 'background 120ms',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--bg-elevated)' }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
          >
            <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--fg-primary)' }}>{r.label}</span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--fg-muted)' }}>{r.sublabel}</span>
          </a>
        ))}
      </div>
    </div>
  )
}

function PlayIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M4 3v8l6-4-6-4z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  )
}
function ApiIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M2 4h10M2 7h6M2 10h8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  )
}
function CodeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M5 4L2 7l3 3M9 4l3 3-3 3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function BookIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M2 3a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3z" stroke="currentColor" strokeWidth="1.2" />
      <path d="M7 2v10" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}
function ShieldIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 1.5L2 3.5v4c0 3 2.2 5 5 5.5 2.8-.5 5-2.5 5-5.5v-4L7 1.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  )
}
function PaintIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="4.5" cy="6" r="0.8" fill="currentColor" />
      <circle cx="9.5" cy="6" r="0.8" fill="currentColor" />
      <circle cx="7" cy="9" r="0.8" fill="currentColor" />
    </svg>
  )
}

function CallToAction() {
  const cliBlock = `# clone + boot
$ git clone https://github.com/JoelJohnsonThomas/forgeflow
$ cd forgeflow && cp .env.example .env

# bring up the stack
$ docker compose up
→ api      :8000  ready
→ mcp      :8001  tools registered
→ postgres :5432  migrated · pgvector OK
→ console  :8501  nginx · proxies /api → api:8000`

  return (
    <section>
      <div className="wrap">
        <div className="cta">
          <div>
            <div className="section-eyebrow">快速开始</div>
            <h2>交付 AI 系统，而非底层管道。</h2>
            <p>
              立即运行开放内核 —— 自托管、Apache 2.0。如果遇到困难，或希望参与塑造路线图，
              欢迎在 GitHub 上提 Issue 或发起 Discussion。
            </p>
            <div className="ctas">
              <a href={CONSOLE_HREF} className="btn primary">
                打开控制台 →
              </a>
              <a href="https://github.com/JoelJohnsonThomas/forgeflow/discussions" target="_blank" rel="noopener noreferrer" className="btn">
                联系工程团队 →
              </a>
            </div>
          </div>
          <pre className="code" style={{ background: 'oklch(0.125 0.010 250 / 0.6)' }}>{cliBlock}</pre>
        </div>
      </div>
    </section>
  )
}

const GITHUB_REPO = 'https://github.com/JoelJohnsonThomas/forgeflow'

type FooterLink = { label: string; href: string; external?: boolean }

const FOOTER_COLUMNS: { title: string; items: FooterLink[] }[] = [
  {
    title: '控制台',
    items: [
      { label: '实时控制台', href: '/console' },
      { label: '实时运行', href: '/console/runs' },
      { label: '审批', href: '/console/approvals' },
      { label: '成本与支出', href: '/console/cost' },
      { label: '审计日志', href: '/console/audit' },
    ],
  },
  {
    title: '开发者',
    items: [
      { label: '文档', href: '/#docs' },
      { label: 'REST API · Swagger', href: '/api/docs', external: true },
      { label: 'OpenAPI 规范', href: '/api/openapi.json', external: true },
      { label: 'GitHub 仓库', href: GITHUB_REPO, external: true },
      { label: '路线图', href: `${GITHUB_REPO}/blob/main/ROADMAP.md`, external: true },
    ],
  },
  {
    title: '运维',
    items: [
      { label: '销售运营生产运行手册', href: `${GITHUB_REPO}/blob/main/docs/sales-ops-production.md`, external: true },
      { label: '架构深度剖析', href: '/architecture' },
      { label: '设计系统', href: '/design-system' },
      { label: '状态', href: '/api/health', external: true },
    ],
  },
  {
    title: '项目',
    items: [
      { label: '项目说明', href: `${GITHUB_REPO}/blob/main/README.md`, external: true },
      { label: '贡献指南', href: `${GITHUB_REPO}/blob/main/CONTRIBUTING.md`, external: true },
      { label: '安全策略', href: `${GITHUB_REPO}/blob/main/SECURITY_AUDIT.md`, external: true },
      { label: '许可证 · Apache 2.0', href: `${GITHUB_REPO}/blob/main/LICENSE`, external: true },
      { label: '行为准则', href: `${GITHUB_REPO}/blob/main/CODE_OF_CONDUCT.md`, external: true },
    ],
  },
]

function Footer() {
  return (
    <footer>
      <div className="wrap">
        <div className="grid">
          <div>
            <Link to="/" className="brand">
              <span className="brand-name">ForgeFlow</span>
            </Link>
            <p style={{ marginTop: 14, maxWidth: '32ch' }}>
              为生产级 AI 智能体打造的操作系统。开放内核，Apache 2.0。
            </p>
            <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
              <a href={`${GITHUB_REPO}/blob/main/LICENSE`} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }}>
                <span className="badge">Apache 2.0</span>
              </a>
              <a href={`${GITHUB_REPO}/releases`} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }}>
                <span className="badge">v0.1.0</span>
              </a>
              <a href="/api/health" target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }} aria-label="打开 API 健康检查端点">
                <span className="badge">
                  API 健康检查
                </span>
              </a>
            </div>
          </div>
          {FOOTER_COLUMNS.map((c) => (
            <FooterCol key={c.title} title={c.title} items={c.items} />
          ))}
        </div>
        <div className="hairline" style={{ marginTop: 40 }} />
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 18, fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '.04em' }}>
          <span>© 2026 ForgeFlow 实验室 · Apache 2.0</span>
          <span>
            <a href={GITHUB_REPO} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>
              github.com/JoelJohnsonThomas/forgeflow
            </a>
          </span>
        </div>
      </div>
    </footer>
  )
}

function FooterCol({ title, items }: { title: string; items: FooterLink[] }) {
  return (
    <div>
      <h5>{title}</h5>
      <ul>
        {items.map((i) => (
          <li key={i.label}>
            {i.external ? (
              <a href={i.href} target="_blank" rel="noopener noreferrer">{i.label}</a>
            ) : (
              <a href={i.href}>{i.label}</a>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
