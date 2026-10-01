import { useEffect } from 'react'
import { Link } from '@tanstack/react-router'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import '../styles/design-hub.css'

export function DesignHubPage() {
  useDocumentTitle('设计探索')
  useEffect(() => {
    document.body.classList.add('design-hub')
    return () => document.body.classList.remove('design-hub')
  }, [])

  return (
    <div className="design-hub-root" data-theme="dark">
      <HubTopStrip />
      <div className="hub">
        <HubTop />
        <StatStrip />
        <Deliverables />
        <DesignMoves />
        <AiNativeFeatures />
        <Positioning />
        <Roadmap />
        <HubFooter />
      </div>
    </div>
  )
}

function HubTopStrip() {
  return (
    <header className="hub-top-strip">
      <div className="inner">
        <Link to="/" className="brand">
          <span className="brand-name">ForgeFlow</span>
        </Link>
        <nav>
          <a href="/design-hub" className="active">索引</a>
          <a href="/">落地页</a>
          <a href="/console">控制台</a>
          <a href="/architecture">架构</a>
          <a href="/design-system">设计系统</a>
        </nav>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-muted)' }}>
          设计 v1 · 2026.05.26
        </div>
      </div>
    </header>
  )
}

function HubTop() {
  return (
    <div className="hub-top">
      <div style={{ flex: 1, maxWidth: 880 }}>
        <div className="section-eyebrow">设计探索 · 企业级 AI 编排</div>
        <h1>为<em>生产级 AI 智能体</em>打造的操作系统。</h1>
        <p className="lede">
          对 ForgeFlow 的一次完整设计梳理 —— 落地页、多屏操作台、系统架构参考，以及将它们统合起来的设计系统。
          深色优先、等宽表格、密度优先。为凌晨两点值守的工程师、身处安全审查中的平台产品经理，
          以及正在评估续约的 CTO 而设计。
        </p>
      </div>
      <div className="right">
        <div><b>4 个界面</b> · 1 套设计系统</div>
        <div>~12 个屏幕 · 8 张系统图</div>
        <div style={{ marginTop: 10 }}>
          <span className="status-bar">
            <span className="dot live" /> v1 · 可供评审
          </span>
        </div>
      </div>
    </div>
  )
}

function StatStrip() {
  return (
    <div className="stat-strip">
      <div className="stat">
        <span className="k">落地页</span>
        <span className="v">9 个章节</span>
        <span className="note">首屏 · 架构 · 信任 · 行动号召</span>
      </div>
      <div className="stat">
        <span className="k">控制台屏幕</span>
        <span className="v">13<em> · 可交互</em></span>
        <span className="note">对接后端 API · 实时数据</span>
      </div>
      <div className="stat">
        <span className="k">系统图</span>
        <span className="v">8<em> · 动效</em></span>
        <span className="note">主控 · A2A · MCP · K8s</span>
      </div>
      <div className="stat">
        <span className="k">设计令牌</span>
        <span className="v">60+<em> · oklch</em></span>
        <span className="note">色彩 · 字体 · 间距 · 动效</span>
      </div>
    </div>
  )
}

type Deliverable = {
  href: string
  iframeSrc: string
  iframeTall?: boolean
  num: string
  title: string
  desc: string
  badges: string[]
}

const DELIVERABLES: Deliverable[] = [
  {
    href: '/',
    iframeSrc: '/',
    iframeTall: true,
    num: '01 · 落地页',
    title: '营销界面 · 首屏、架构、平台、信任',
    desc: '首屏区在留白处浮动着一个实时迷你控制台。亚秒级可观测性预览、动效主控拓扑、深入的企业级与气隙隔离叙事。为打动 SRE 与安全审查者而设计，而非只取悦采购方。',
    badges: ['9 个章节', '动效架构图', '企业级叙事'],
  },
  {
    href: '/console',
    iframeSrc: '/console/runs',
    num: '02 · 控制台',
    title: '操作台 · 13 个可导航屏幕',
    desc: '落地即是一次实时运行 —— 最抓人的一屏 —— 包含甘特图、事件流、工具火焰追踪、审批卡片、记忆召回、状态差异、智能体名册。另有概览、审批队列、智能体拓扑、成本、评估、审计、集群、记忆。',
    badges: ['13 个屏幕', '对接 FastAPI', '实时 SSE 手感'],
  },
  {
    href: '/architecture',
    iframeSrc: '/architecture',
    iframeTall: true,
    num: '03 · 架构',
    title: '系统参考 · 8 张可视化图',
    desc: '主控编排 · A2A 协议 · MCP 拓扑 · 语义记忆图 · 检查点状态 · 事件流 · K8s 容器组编排 · 带气隙隔离的多区域。那份会被钉在平台团队文档里的产物。',
    badges: ['8 张图', '动效数据包', '实现规格'],
  },
  {
    href: '/design-system',
    iframeSrc: '/design-system',
    iframeTall: true,
    num: '04 · 设计系统',
    title: '设计令牌、字体、组件、动效',
    desc: '色彩（oklch）、Geist + JetBrains Mono 搭配、间距尺度、圆角、层级、智能体头像、工作流节点原语、动效原则。每个界面都由这些设计令牌组合而成 —— 改一处，全局生效。',
    badges: ['60+ 个设计令牌', '5 组组件', '3 条动效曲线'],
  },
]

function Deliverables() {
  return (
    <>
      <div className="sec-h">
        <span className="num">01</span>
        <h2>交付物</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>点击任意卡片可全尺寸打开</span>
      </div>
      <div className="hub-grid">
        {DELIVERABLES.slice(0, 2).map((d) => (
          <DeliverableCard key={d.href} d={d} />
        ))}
      </div>
      <div className="hub-grid" style={{ marginTop: 16 }}>
        {DELIVERABLES.slice(2).map((d) => (
          <DeliverableCard key={d.href} d={d} />
        ))}
      </div>
    </>
  )
}

function DeliverableCard({ d }: { d: Deliverable }) {
  return (
    <a className="deliverable" href={d.href}>
      <div className="preview">
        <div className="pad">
          <iframe src={d.iframeSrc} className={d.iframeTall ? 'tall' : ''} loading="lazy" title={d.title} />
        </div>
      </div>
      <div className="meta">
        <div className="label">
          <span>{d.num}</span>
          <span className="arrow">→</span>
        </div>
        <div className="title">{d.title}</div>
        <p className="desc">{d.desc}</p>
      </div>
      <div className="footer">
        {d.badges.map((b) => (
          <span key={b} className="badge">{b}</span>
        ))}
      </div>
    </a>
  )
}

const DESIGN_MOVES = [
  { num: '01 · 深色优先，石墨而非纯黑', title: '控制台并不假装自己是一本笔记本。', body: '深石墨色画布为 oklch(0.165) —— 绝非纯黑，也绝不用高彩度石板色。它读起来像一块仪表盘；在凌晨两点的值守班次中依然耐看，不会让眼睛疲劳。' },
  { num: '02 · 全表格化', title: '数字始终对齐。', body: '每一处成本、延迟、评分、Token 计数都使用等宽数字。视线沿列下行而不会跳动。等宽也是那句视觉低语：「这出自系统，而非人手。」' },
  { num: '03 · 动效指向状态', title: '实时指示器脉动，界面本身不动。', body: '唯一让动效「声量」拉满的地方，是运行中工作流的微光，以及穿梭于拓扑图的 SSE 数据包 —— 恰好是用户需要知道「有东西活着」之处。悬停状态在 180ms 内落定。' },
  { num: '04 · 智能体是一等公民', title: '同一个头像，出现在四处。', body: '研究员的紫色标记在拓扑图、甘特图、审计日志与审批卡片中完全一致。操作者识别智能体，就如同 SRE 识别服务一样。' },
  { num: '05 · 密度优先于装饰', title: '用发丝线，而非层层套卡的卡片。', body: '1px 的微妙边框、12–16px 的间距、不堆叠阴影、数据界面不用渐变填充。信息本身就是设计；装饰则让开路。' },
  { num: '06 · AI 原生的 UX 触点', title: 'ForgeFlow AI 助手栖居于成本与评估视图。', body: '评估页面上有根因助手；成本页面上有带「应用策略」行动号召的预测性成本估算；快捷命令面板提供「询问 ForgeFlow AI」与「模拟成本变化」—— 把操作者 AI 当作一个动作，而非聊天机器人。' },
]

function DesignMoves() {
  return (
    <>
      <div className="sec-h">
        <span className="num">02</span>
        <h2>真正重要的设计手法</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>为何它读起来像企业级 AI 基础设施，而非泛用通用软件服务</span>
      </div>
      <div className="wins">
        {DESIGN_MOVES.map((m) => (
          <div key={m.num} className="w">
            <div className="num">{m.num}</div>
            <h4>{m.title}</h4>
            <p>{m.body}</p>
          </div>
        ))}
      </div>
    </>
  )
}

type FeatureStatus = 'preview' | 'planned'
const AI_FEATURES: { status: FeatureStatus; title: string; body: string }[] = [
  { status: 'preview', title: 'ForgeFlow AI · 根因助手', body: '评估页面上一块经过设计的面板，对失败类别进行归组并给出修复建议。目前建议为示意性质；自动化分析尚未接通。' },
  { status: 'preview', title: '预测性成本估算', body: '月末支出预测，配预算条与模型替换建议。预测数字为示意性质；实时支出显示在其下方的面板中。' },
  { status: 'planned', title: '自然语言运行检索', body: '规划中：一个快捷命令面板，用于检索运行、智能体、审计与记忆，并把「询问 ForgeFlow AI」与「模拟成本变化」作为一等动作。当前检索链接到审计日志。' },
  { status: 'planned', title: '检查点处回放与分叉', body: '运行会在每个节点持久化一个 Postgres 检查点。用于从检查点回放或分叉的控制台界面尚在规划；运行页的「重放」控件尚未接通。' },
  { status: 'planned', title: '工作流仿真模式', body: '在推送变更之前，针对历史流量对工作流进行试运行。输出成本、评判模型评分与幻觉率的 A/B 视图。' },
  { status: 'planned', title: '工作流自愈', body: '当某个工具的熔断器触发时，主控回退到已声明的备选方案 —— 在审计中呈现，并可在控制台中回放。' },
  { status: 'planned', title: 'AI 治理中心', body: '策略包、模型白名单、对每次工具输入做敏感信息检测，以及签名检查点 —— 「最小权限」的 AI 版本。' },
  { status: 'planned', title: '语义可观测性', body: '按意图而非仅按字符串检索审计与追踪。「给我看研究员撞上付费墙的那些运行」即可返回正确的转录。' },
]

function statusPill(status: FeatureStatus) {
  return status === 'preview'
    ? <span className="new" style={{ background: 'oklch(0.30 0.06 75 / 0.45)', color: 'var(--amber-4)' }}>预览</span>
    : <span className="new" style={{ background: 'var(--bg-elevated)', color: 'var(--fg-muted)' }}>规划中</span>
}

function AiNativeFeatures() {
  return (
    <>
      <div className="sec-h">
        <span className="num">03</span>
        <h2>设计中的 AI 原生手法</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>预览 = 已设计并可见 · 规划中 = 尚未构建</span>
      </div>
      <div className="features-tight">
        {AI_FEATURES.map((f) => (
          <div key={f.title} className="f">
            <h5>
              {statusPill(f.status)}
              {f.title}
            </h5>
            <p>{f.body}</p>
          </div>
        ))}
      </div>
    </>
  )
}

type Mark = 'check' | 'partial' | 'miss'
type PosRow = { capability: string; cells: { mark: Mark; label: string }[] }

const POSITIONING: PosRow[] = [
  { capability: '多智能体编排', cells: [
    { mark: 'check', label: '● 生产级 · 受监督' },
    { mark: 'partial', label: '● 演示 / 框架' },
    { mark: 'partial', label: '● 工作室 · 非生产' },
    { mark: 'miss', label: '● 仅工作流 · 无智能体循环' },
    { mark: 'miss', label: '●' },
  ]},
  { capability: '人工介入', cells: [
    { mark: 'check', label: '● 带类型审批 · 可中断前挂起' },
    { mark: 'partial', label: '● 自带实现' },
    { mark: 'partial', label: '●' },
    { mark: 'check', label: '●' },
    { mark: 'check', label: '● 界面构建器' },
  ]},
  { capability: 'LLM 评估', cells: [
    { mark: 'check', label: '● 评判模型 + 数据集 · 内置' },
    { mark: 'check', label: '●' },
    { mark: 'miss', label: '●' },
    { mark: 'miss', label: '●' },
    { mark: 'miss', label: '●' },
  ]},
  { capability: '成本与预算护栏', cells: [
    { mark: 'check', label: '● 按智能体 · 预测 · 熔断' },
    { mark: 'partial', label: '● 仅成本' },
    { mark: 'miss', label: '●' },
    { mark: 'miss', label: '●' },
    { mark: 'partial', label: '● 仅基础设施' },
  ]},
  { capability: '气隙隔离部署', cells: [
    { mark: 'check', label: '● Ollama · 签名包' },
    { mark: 'miss', label: '● 云服务优先' },
    { mark: 'partial', label: '● 自托管' },
    { mark: 'check', label: '●' },
    { mark: 'miss', label: '●' },
  ]},
  { capability: '企业级治理', cells: [
    { mark: 'check', label: '● RBAC · 审计 · OIDC · 多因素认证' },
    { mark: 'partial', label: '● 云 RBAC' },
    { mark: 'miss', label: '●' },
    { mark: 'check', label: '●' },
    { mark: 'check', label: '●' },
  ]},
  { capability: '生产级可观测性 UI', cells: [
    { mark: 'check', label: '● 实时控制台 + 快捷命令面板' },
    { mark: 'partial', label: '● 追踪查看器' },
    { mark: 'miss', label: '●' },
    { mark: 'partial', label: '● 工作流 UI' },
    { mark: 'check', label: '● 不具备 AI 感知' },
  ]},
]

function Positioning() {
  return (
    <>
      <div className="sec-h">
        <span className="num">04</span>
        <h2>竞争定位</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>ForgeFlow 与相邻工具的对比</span>
      </div>
      <div className="positioning">
        <div className="prow">
          <div className="pcell">能力</div>
          <div className="pcell">ForgeFlow</div>
          <div className="pcell">LangSmith / CrewAI</div>
          <div className="pcell">AutoGen Studio</div>
          <div className="pcell">Temporal · Airflow</div>
          <div className="pcell">Datadog · Retool</div>
        </div>
        {POSITIONING.map((row) => (
          <div className="prow" key={row.capability}>
            <div className="pcell">{row.capability}</div>
            {row.cells.map((c, i) => (
              <div className="pcell" key={i}>
                <span className={c.mark}>{c.label}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  )
}

const ROADMAP_COLS = [
  { cls: '', tag: '正在交付', when: 'v0.1 · 当前', items: [
    '实时控制台（当前构建）—— 运行、智能体、审批',
    '针对本地 Ollama 守护进程的气隙隔离部署',
    '用于可恢复运行的 Postgres 检查点',
    'RBAC 角色 + 限定范围 API Token + 审计日志',
  ]},
  { cls: 'q2', tag: '下一步', when: 'v0.2 · 规划中', items: [
    '工作流仿真模式（针对历史流量回放）',
    '拖拽式图编辑器（React Flow）',
    '技能市场 v2 —— 签名的社区模板',
    'ForgeFlow AI · 故障根因助手 GA',
  ]},
  { cls: 'q3', tag: '已设计', when: 'v0.3 · 已设计', items: [
    '自然语言创建工作流',
    '工作流自愈（工具自动回退）',
    '自改进编排（在线主控）',
    'SDK · TypeScript + Go 与 Python 对齐',
  ]},
  { cls: 'q4', tag: '探索中', when: '未来 · 探索中', items: [
    '智能体协作可视化回放（可时间拖动）',
    '语义可观测性 —— 按意图检索',
    'AI 治理中心 · 模型 + 工具白名单',
    '原生语音 + 多模态智能体输入',
  ]},
]

function Roadmap() {
  return (
    <>
      <div className="sec-h">
        <span className="num">05</span>
        <h2>接下来交付什么</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>设计 + 产品路线图 · 未来 4 个季度</span>
      </div>
      <div className="roadmap">
        {ROADMAP_COLS.map((c) => (
          <div key={c.tag} className={`col ${c.cls}`}>
            <div className="q">{c.tag}</div>
            <div className="when">{c.when}</div>
            <ul>
              {c.items.map((it) => <li key={it}>{it}</li>)}
            </ul>
          </div>
        ))}
      </div>
    </>
  )
}

function HubFooter() {
  return (
    <div
      style={{
        marginTop: 80,
        paddingTop: 32,
        borderTop: '1px solid var(--border-subtle)',
        display: 'flex',
        justifyContent: 'space-between',
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        color: 'var(--fg-muted)',
      }}
    >
      <span>ForgeFlow · 设计探索 v1 · 2026.05.26</span>
      <span>4 项交付物 · 落地页 · 控制台 · 架构 · 设计系统</span>
    </div>
  )
}
