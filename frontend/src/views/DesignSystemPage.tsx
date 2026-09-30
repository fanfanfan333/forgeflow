import { useEffect } from 'react'
import { Link } from '@tanstack/react-router'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import '../styles/design-system.css'

export function DesignSystemPage() {
  useDocumentTitle('设计系统')
  useEffect(() => {
    document.body.classList.add('design-system')
    return () => document.body.classList.remove('design-system')
  }, [])

  return (
    <div className="design-system-root" data-theme="dark">
      <DsNav />
      <div className="ds-page">
        <DsHero />
        <ColorSection />
        <TypeSection />
        <SpaceSection />
        <ComponentsSection />
        <MotionSection />
        <ImplementationSection />
      </div>
    </div>
  )
}

function DsNav() {
  return (
    <header className="ds-nav">
      <div className="inner">
        <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
          <Link to="/" className="brand">
            <span className="brand-name">ForgeFlow</span>
          </Link>
          <ul>
            <li><a href="/design-hub">索引</a></li>
            <li><a href="/">落地页</a></li>
            <li><a href="/console">控制台</a></li>
            <li><a href="/architecture">架构</a></li>
            <li><a href="/design-system" className="active">设计系统</a></li>
          </ul>
        </div>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)' }}>
          ds · v1.0
        </div>
      </div>
    </header>
  )
}

function DsHero() {
  return (
    <section className="ds-hero">
      <div className="section-eyebrow">设计系统 · v1.0</div>
      <h1>我们用来构建 ForgeFlow 的语言。</h1>
      <p className="lede">
        Token、字体、组件、动效。同一套界面语言，供每个团队 —— 落地页、控制台、市场、文档、内部工具 ——
        组合使用。为高信息密度而优化，不带来视觉噪音；为操作者与工程师而设计；为凌晨两点的暗室而设计。
      </p>
      <div className="ds-toc">
        <a href="#color"><span className="label">01</span><span className="name">色彩与信号</span></a>
        <a href="#type"><span className="label">02</span><span className="name">字体排印</span></a>
        <a href="#space"><span className="label">03</span><span className="name">间距、圆角、栅格</span></a>
        <a href="#components"><span className="label">04</span><span className="name">组件</span></a>
        <a href="#motion"><span className="label">05</span><span className="name">动效</span></a>
      </div>
    </section>
  )
}

const SURFACE_SWATCHES = [
  { name: 'bg-page', hex: 'oklch(0.145 0.012 250)', bg: 'var(--bg-page)' },
  { name: 'bg-canvas', hex: 'oklch(0.165 0.012 250)', bg: 'var(--bg-canvas)' },
  { name: 'bg-elevated', hex: 'oklch(0.205 0.013 250)', bg: 'var(--bg-elevated)' },
  { name: 'bg-overlay', hex: 'oklch(0.235 0.014 250)', bg: 'var(--bg-overlay)' },
  { name: 'bg-inset', hex: 'oklch(0.125 0.010 250)', bg: 'var(--bg-inset)' },
  { name: 'border', hex: 'oklch(0.305 0.012 250)', bg: 'var(--border-default)' },
]

const FOREGROUND_SWATCHES = [
  { name: 'fg-primary', hex: 'oklch(0.965)', bg: 'var(--fg-primary)' },
  { name: 'fg-secondary', hex: 'oklch(0.82)', bg: 'var(--fg-secondary)' },
  { name: 'fg-muted', hex: 'oklch(0.62)', bg: 'var(--fg-muted)' },
  { name: 'fg-subtle', hex: 'oklch(0.48)', bg: 'var(--fg-subtle)' },
  { name: 'fg-faint', hex: 'oklch(0.36)', bg: 'var(--fg-faint)' },
]

const SIGNAL_RAMPS = [
  { name: '蓝色 · 主色', color: 'var(--blue-4)', sub: '动作 · 主控 · 路由', stops: ['var(--blue-1)', 'var(--blue-2)', 'var(--blue-3)', 'var(--blue-4)', 'var(--blue-5)'] },
  { name: '紫色 · 研究', color: 'var(--purple-4)', sub: '富化 · A2A', stops: ['var(--purple-1)', 'var(--purple-2)', 'var(--purple-3)', 'var(--purple-4)'] },
  { name: '翠绿 · 成功', color: 'var(--emerald-4)', sub: '健康 · 已完成', stops: ['var(--emerald-1)', 'var(--emerald-2)', 'var(--emerald-3)', 'var(--emerald-4)'] },
  { name: '琥珀 · 警告', color: 'var(--amber-4)', sub: '审批 · 降级', stops: ['var(--amber-1)', 'var(--amber-2)', 'var(--amber-3)', 'var(--amber-4)'] },
  { name: '红色 · 严重', color: 'var(--red-4)', sub: '失败 · 越界 · 熔断', stops: ['var(--red-1)', 'var(--red-2)', 'var(--red-3)', 'var(--red-4)'] },
]

function Swatch({ name, hex, bg }: { name: string; hex: string; bg: string }) {
  return (
    <div className="swatch">
      <div className="color" style={{ background: bg }} />
      <div className="meta">
        <div className="name">{name}</div>
        <div className="hex">{hex}</div>
      </div>
    </div>
  )
}

function ColorSection() {
  return (
    <section className="ds-section" id="color">
      <h2><span className="num">01</span>色彩与信号</h2>
      <p className="lede">
        深石墨画布、暖白前景，五种信号色相调到同一彩度，让它们在视觉权重上等量齐观。均以 oklch 编写。
      </p>

      <div className="section-eyebrow" style={{ marginTop: 8 }}>表面</div>
      <div className="palette" style={{ marginTop: 14 }}>
        {SURFACE_SWATCHES.map((s) => <Swatch key={s.name} {...s} />)}
      </div>

      <div className="section-eyebrow" style={{ marginTop: 28 }}>前景</div>
      <div className="palette" style={{ marginTop: 14 }}>
        {FOREGROUND_SWATCHES.map((s) => <Swatch key={s.name} {...s} />)}
      </div>

      <div className="section-eyebrow" style={{ marginTop: 28 }}>信号</div>
      <div className="ds-swatch-grid">
        {SIGNAL_RAMPS.map((r) => (
          <div className="swatch" key={r.name}>
            <div style={{ display: 'grid', gridTemplateRows: `repeat(${r.stops.length}, 1fr)`, aspectRatio: '1.4' }}>
              {r.stops.map((stop, i) => <div key={i} style={{ background: stop }} />)}
            </div>
            <div className="meta">
              <div className="name" style={{ color: r.color }}>{r.name}</div>
              <div className="hex">{r.sub}</div>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function TypeSection() {
  return (
    <section className="ds-section" id="type">
      <h2><span className="num">02</span>字体排印</h2>
      <p className="lede">
        两种字体：Geist 用于全部 UI，JetBrains Mono 用于 ID、代码、数字，以及任何我们想低语「这是数据」的场合。
        处于表格中的数字一律使用表格数字。
      </p>

      <div className="ds-type-grid">
        <div>
          <div className="section-eyebrow">展示 · Geist</div>
          <div style={{ marginTop: 8 }}>
            <div style={{ fontSize: 60, fontWeight: 500, letterSpacing: '-0.035em', lineHeight: 1.05, color: 'var(--fg-primary)' }}>
              Aa Bb Cc 123
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)', marginTop: 8 }}>
              Geist · 300 · 400 · 500 · 600 · 700
            </div>
          </div>
        </div>
        <div>
          <div className="section-eyebrow">等宽 · JetBrains Mono</div>
          <div style={{ marginTop: 8 }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 48, fontWeight: 500, color: 'var(--fg-primary)' }}>
              Aa Bb 0123 ≠
            </div>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)', marginTop: 8 }}>
              JetBrains Mono · 400 · 500 · 600 · 带零斜杠
            </div>
          </div>
        </div>
      </div>

      <div style={{ marginTop: 36, background: 'var(--bg-canvas)', border: '1px solid var(--border-subtle)', borderRadius: 'var(--r-3)', padding: '8px 24px' }}>
        <TypeSpec lbl="展示 / 72" spec="geist · 500 · -3.5% · 1.0">
          <span style={{ fontSize: 72, fontWeight: 500, letterSpacing: '-0.035em', lineHeight: 1, color: 'var(--fg-primary)' }}>
            生产级 AI Agent
          </span>
        </TypeSpec>
        <TypeSpec lbl="H1 / 48" spec="geist · 500 · -3% · 1.05">
          <span style={{ fontSize: 48, fontWeight: 500, letterSpacing: '-0.03em', lineHeight: 1.05, color: 'var(--fg-primary)' }}>
            像运维技术栈其他部分一样运维 AI。
          </span>
        </TypeSpec>
        <TypeSpec lbl="H2 / 24" spec="geist · 500 · -1.8% · 1.2">
          <span style={{ fontSize: 24, fontWeight: 500, letterSpacing: '-0.018em', lineHeight: 1.2, color: 'var(--fg-primary)' }}>
            审批队列
          </span>
        </TypeSpec>
        <TypeSpec lbl="正文 / 14" spec="geist · 400 · 1.55">
          <span style={{ fontSize: 14, lineHeight: 1.55, color: 'var(--fg-secondary)', maxWidth: '56ch' }}>
            ForgeFlow 编排贯穿整个业务的专职 Agent 团队 —— 具备人工介入审批、语义记忆与审计轨迹。
          </span>
        </TypeSpec>
        <TypeSpec lbl="说明 / 11" spec="jbmono · 500 · +12% · uppercase">
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '.12em', textTransform: 'uppercase', color: 'var(--fg-muted)' }}>
            运行 · WF_8K42N · 12.4S
          </span>
        </TypeSpec>
        <TypeSpec lbl="数字 / 24" spec="jbmono · tabular · 0.04">
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 24, color: 'var(--fg-primary)', fontVariantNumeric: 'tabular-nums' }}>
            ¥4,148.20 · 99.992%
          </span>
        </TypeSpec>
      </div>
    </section>
  )
}

function TypeSpec({ lbl, spec, children }: { lbl: string; spec: string; children: React.ReactNode }) {
  return (
    <div className="type-spec">
      <div className="lbl">{lbl}</div>
      <div>{children}</div>
      <div className="spec">{spec}</div>
    </div>
  )
}

const SPACING_SCALE = [
  { size: 4, label: '4 · s-1' },
  { size: 8, label: '8 · s-2' },
  { size: 12, label: '12 · s-3' },
  { size: 16, label: '16 · s-4' },
  { size: 24, label: '24 · s-6' },
  { size: 32, label: '32 · s-8' },
  { size: 48, label: '48 · s-12' },
  { size: 64, label: '64 · s-16' },
  { size: 96, label: '96 · s-24' },
]
const RADII = [
  { r: 3, label: '3 · r-1' },
  { r: 5, label: '5 · r-2' },
  { r: 8, label: '8 · r-3' },
  { r: 12, label: '12 · r-4' },
  { r: 16, label: '16 · r-5' },
  { r: 999, label: '∞ · 胶囊' },
]
const ELEVATIONS = [
  { shadow: 'var(--shadow-sm)', label: 'SHADOW-SM', desc: '卡片 · 输入框 · 低层级面板' },
  { shadow: 'var(--shadow-md)', label: 'SHADOW-MD', desc: '浮层 · 菜单 · 抬升面板' },
  { shadow: 'var(--shadow-lg)', label: 'SHADOW-LG', desc: '模态框 · 命令面板 · 抽屉', overlay: true },
  { shadow: 'var(--shadow-glow-blue)', label: 'GLOW · BLUE', desc: '主行动号召 · 实时状态 · 焦点环', glow: true },
]

function SpaceSection() {
  return (
    <section className="ds-section" id="space">
      <h2><span className="num">03</span>间距、圆角、栅格</h2>
      <p className="lede">
        4px 基准。触控界面落在 8 的倍数上。发丝线为 1px。圆角从小到中；产品 UI 中我们从不圆过 16px ——
        过于柔和是错误的调性。
      </p>

      <div className="ds-spec-grid">
        <div>
          <div className="section-eyebrow">间距尺度</div>
          <div className="spacing-row">
            {SPACING_SCALE.map((s) => (
              <div className="sp-cell" key={s.label}>
                <div className="box" style={{ width: s.size, height: s.size }} />
                <div className="lbl">{s.label}</div>
              </div>
            ))}
          </div>

          <div className="section-eyebrow" style={{ marginTop: 32 }}>圆角</div>
          <div className="radii" style={{ marginTop: 14 }}>
            {RADII.map((r) => (
              <div className="ra-cell" key={r.label}>
                <div className="box" style={{ borderRadius: r.r }} />
                <div className="lbl">{r.label}</div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="section-eyebrow">层级</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 14 }}>
            {ELEVATIONS.map((e) => (
              <div
                key={e.label}
                style={{
                  background: e.overlay ? 'var(--bg-overlay)' : 'var(--bg-canvas)',
                  border: e.glow ? '1px solid var(--blue-3)' : (e.overlay ? '1px solid var(--border-default)' : '1px solid var(--border-subtle)'),
                  borderRadius: 8,
                  padding: 14,
                  boxShadow: e.shadow,
                }}
              >
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '.12em', textTransform: 'uppercase', color: e.glow ? 'var(--blue-4)' : 'var(--fg-muted)' }}>
                  {e.label}
                </div>
                <div style={{ marginTop: 4, fontSize: 12.5, color: 'var(--fg-secondary)' }}>{e.desc}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

const AGENTS = [
  { color: 'blue', initials: '主', name: '主控', role: '路由 · 结构化' },
  { color: 'purple', initials: '研', name: '研究员', role: '网络 · 抓取' },
  { color: 'emerald', initials: '析', name: '分析器', role: '评分 · 理想客户画像' },
  { color: 'amber', initials: '执', name: '执行器', role: '写入 · 邮件' },
  { color: 'muted', initials: '审', name: '人工审批', role: '中断' },
  { color: 'red', initials: '安', name: '安全闸门', role: '策略 · 敏感信息' },
]

function ComponentsSection() {
  return (
    <section className="ds-section" id="components">
      <h2><span className="num">04</span>组件</h2>
      <p className="lede">
        控制台与落地界面据此组合而成的构建块。上方是 Token，下方是组件 —— 改一个 Token，每个组件随之改变。
      </p>

      <div className="section-eyebrow" style={{ marginBottom: 14 }}>按钮</div>
      <div className="demo-grid">
        <div className="demo">
          <div className="hd"><div className="title">主按钮与次按钮</div><div className="meta">样式类：主按钮 / 次按钮</div></div>
          <div className="body">
            <button className="btn primary">批准 · 恢复</button>
            <button className="btn">重放</button>
            <button className="btn ghost">取消</button>
            <button className="btn primary sm">确认</button>
            <button className="btn sm">筛选</button>
          </div>
        </div>
        <div className="demo">
          <div className="hd"><div className="title">状态</div><div className="meta">悬停 · 禁用 · 加载中</div></div>
          <div className="body">
            <button className="btn primary">默认</button>
            <button className="btn primary" style={{ filter: 'brightness(1.08)' }}>悬停</button>
            <button className="btn primary" style={{ opacity: 0.5, pointerEvents: 'none' }}>禁用</button>
            <button className="btn">
              <svg width="13" height="13" viewBox="0 0 13 13">
                <circle cx="6.5" cy="6.5" r="4" stroke="currentColor" strokeWidth="1.4" fill="none" strokeDasharray="6 4">
                  <animateTransform attributeName="transform" type="rotate" from="0 6.5 6.5" to="360 6.5 6.5" dur="1s" repeatCount="indefinite" />
                </circle>
              </svg>
              运行中…
            </button>
          </div>
        </div>
      </div>

      <div className="section-eyebrow" style={{ marginTop: 32, marginBottom: 14 }}>徽章与状态</div>
      <div className="demo">
        <div className="body">
          <span className="badge"><span className="dot live" /> 实时</span>
          <span className="badge emerald">● 已完成</span>
          <span className="badge amber">● 待审批</span>
          <span className="badge red">● 失败</span>
          <span className="badge blue">● 运行中</span>
          <span className="badge purple">● 气隙隔离</span>
          <span className="badge">销售线索资质评估</span>
          <span className="badge mono">运行 ID #8K42</span>
          <span className="status-bar"><span className="dot live" /> 12 次运行 · 1.8k 事件/秒</span>
        </div>
      </div>

      <div className="section-eyebrow" style={{ marginTop: 32, marginBottom: 14 }}>智能体头像</div>
      <p style={{ color: 'var(--fg-muted)', fontSize: 12.5, marginBottom: 14 }}>
        中文单字等宽标记，按角色着色。在整个控制台中保持一致 —— 同一个研究员，在拓扑图、时间线、
        审计与审批卡片中都读作同一个研究员。
      </p>
      <div className="agent-grid">
        {AGENTS.map((a) => (
          <div key={a.name} className="agent-card">
            <div className={`av-lg ${a.color}`}>{a.initials}</div>
            <div className="name">{a.name}</div>
            <div className="role">{a.role}</div>
          </div>
        ))}
      </div>

      <div className="section-eyebrow" style={{ marginTop: 32, marginBottom: 14 }}>KPI 与面板</div>
      <div className="kpi-strip">
        <div className="kpi"><span className="label">运行 · 24h</span><span className="val">2,418</span><span className="delta up">▲ 14.2%</span></div>
        <div className="kpi"><span className="label">成功率</span><span className="val">99.3<span className="u">%</span></span><span className="delta">稳定</span></div>
        <div className="kpi"><span className="label">支出</span><span className="val">¥184.20</span><span className="delta down">▼ 8%</span></div>
        <div className="kpi"><span className="label">p50 墙钟</span><span className="val">11.8<span className="u">s</span></span><span className="delta">p95: 28.4s</span></div>
        <div className="kpi"><span className="label">评审</span><span className="val" style={{ color: 'var(--emerald-4)' }}>8.9<span className="u">/10</span></span><span className="delta">幻觉 0.08%</span></div>
      </div>
    </section>
  )
}

function MotionSection() {
  return (
    <section className="ds-section" id="motion">
      <h2><span className="num">05</span>动效</h2>
      <p className="lede">
        动效是状态指针，不是愉悦系统。三种时长，三条曲线。动画在 320ms 内落定；实时指示器以 1.6s 脉动；
        绝不弹跳，绝不装饰。
      </p>

      <div className="motion-grid">
        <div className="motion-card ease-out">
          <div className="label">缓出 · UI</div>
          <div className="desc">
            悬停、面板滑入、焦点切换的默认值。<br />
            <span className="mono" style={{ color: 'var(--blue-4)' }}>cubic-bezier(0.16, 1, 0.3, 1)</span> · 180ms
          </div>
          <div className="stage"><div className="ball" /></div>
        </div>
        <div className="motion-card ease-spring">
          <div className="label">弹簧 · 数据</div>
          <div className="desc">
            用于数据可视化 —— 条形落定、仪表更新、图表重绘。<br />
            <span className="mono" style={{ color: 'var(--blue-4)' }}>cubic-bezier(0.2, 0.8, 0.2, 1)</span> · 320ms
          </div>
          <div className="stage"><div className="ball" /></div>
        </div>
        <div className="motion-card ease-linear">
          <div className="label">线性 · 流</div>
          <div className="desc">
            流式指示器、拓扑中的数据包路径、运行中任务的微光。<br />
            <span className="mono" style={{ color: 'var(--blue-4)' }}>linear</span> · 1.6s 循环
          </div>
          <div className="stage"><div className="ball" /></div>
        </div>
      </div>

      <div className="ds-principles">
        <Principle eyebrow="原则 01" body={<>用动效传达状态，而非个性。一颗脉动的圆点表示「活着」；一根动画中的条形表示「仍在处理」。</>} />
        <Principle eyebrow="原则 02" body={<>尊重 <span className="mono">prefers-reduced-motion</span>。用透明度变化替代位移；绝不把功能依赖在动画上。</>} />
        <Principle eyebrow="原则 03" body={<>延迟预算。UI 过渡 ≤ 200ms。数据可视化更新 ≤ 400ms。更长的耗时都需要进度提示，而不是让用户干等。</>} />
      </div>
    </section>
  )
}

function Principle({ eyebrow, body }: { eyebrow: string; body: React.ReactNode }) {
  return (
    <div>
      <div className="section-eyebrow">{eyebrow}</div>
      <p style={{ marginTop: 6, fontSize: 13, color: 'var(--fg-secondary)' }}>{body}</p>
    </div>
  )
}

function ImplementationSection() {
  return (
    <section className="ds-section" style={{ borderBottom: 'none' }}>
      <h2><span className="num">06</span>实现</h2>
      <p className="lede">设计令牌如何落到代码中，以及我们构建控制台所用的技术栈。</p>
      <div className="ds-impl">
        <div>
          <div className="section-eyebrow">运行时</div>
          <table className="kv" style={{ marginTop: 8 }}>
            <tbody>
              <tr><td>UI</td><td>React 19 · TS</td></tr>
              <tr><td>样式</td><td>设计令牌样式表 · oklch 色彩变量</td></tr>
              <tr><td>状态</td><td>TanStack Query · 组件状态</td></tr>
              <tr><td>实时</td><td>SSE via nginx · /workflows/{`{id}`}/stream</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <div className="section-eyebrow">图形与图表</div>
          <table className="kv" style={{ marginTop: 8 }}>
            <tbody>
              <tr><td>工作流节点</td><td>手写 SVG</td></tr>
              <tr><td>拓扑</td><td>SVG + animateMotion 数据包</td></tr>
              <tr><td>图表</td><td>内联 SVG 路径 · CSS 渐变</td></tr>
              <tr><td>追踪火焰图</td><td>堆叠块状条形</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <div className="section-eyebrow">质量</div>
          <table className="kv" style={{ marginTop: 8 }}>
            <tbody>
              <tr><td>无障碍目标</td><td>WCAG 2.2 AA</td></tr>
              <tr><td>性能预算</td><td>LCP &lt; 1.2s · INP &lt; 100ms</td></tr>
              <tr><td>设计令牌</td><td>单一来源：设计令牌样式表</td></tr>
              <tr><td>视觉回归</td><td>Playwright 截图</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
