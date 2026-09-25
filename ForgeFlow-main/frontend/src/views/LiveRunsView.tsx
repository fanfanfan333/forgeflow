/**
 * Live Runs — the design's showcase view.
 * Backend doesn't expose a "current run with full timeline" endpoint yet, so this
 * renders the same demo content (wf_8K42n) from the design while still pulling
 * real /metrics/runs in the side panel for the most recent run.
 */
import { useRecentRuns } from '../api/hooks'

export function LiveRunsView() {
  return (
    <section className="view active" data-screen-label="Run · wf_8K42n">
      <RunHeader />
      <div className="page-body">
        <DemoBanner />
        <KpiStrip />
        <div className="run-layout" style={{ marginTop: 16 }}>
          <div className="grid-stack">
            <GanttChart />
            <EventStream />
            <ToolTraceTree />
          </div>
          <div className="grid-stack">
            <ApprovalCard />
            <MemoryRecallPanel />
            <StateDiffPanel />
            <AgentsOnRunPanel />
          </div>
        </div>
      </div>
    </section>
  )
}

function DemoBanner() {
  return (
    <div
      role="note"
      style={{
        marginBottom: 16,
        padding: '10px 14px',
        borderLeft: '2px solid var(--amber-4)',
        background: 'var(--bg-inset)',
        borderRadius: 6,
        fontSize: 12.5,
        color: 'var(--fg-secondary)',
        lineHeight: 1.5,
      }}
    >
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '.12em',
          textTransform: 'uppercase',
          color: 'var(--amber-4)',
          marginRight: 8,
        }}
      >
        示例运行
      </span>
      本页展示一次示例运行（<span className="mono">wf_8K42n</span>）—— 时间线、事件流、工具调用轨迹与
      审批卡片均为示意。逐运行的实时流式端点（<span className="mono">/workflows/&#123;id&#125;/stream</span>）尚在规划中；
      当存在真实运行时，上方标题会反映你最近一次的真实运行。
    </div>
  )
}

function RunHeader() {
  const runs = useRecentRuns(1)
  const r = runs.data?.[0]
  const title = r ? `${r.workflow_type} · ${r.run_id.slice(0, 8)}` : 'Stripe — E 轮扩展线索'
  return (
    <div className="page-head">
      <div className="row">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <h1>{title}</h1>
            <span className="badge amber">
              <span
                className="dot"
                style={{
                  background: 'var(--amber-4)',
                  boxShadow: '0 0 6px oklch(0.78 0.16 75 / 0.6)',
                }}
              />{' '}
              等待审批
            </span>
            <span className="badge">
              <span className="mono">{r?.workflow_type ?? 'sales_ops'}</span>
            </span>
          </div>
          <p className="sub mono">
            {r
              ? `${r.run_id.slice(0, 8)} · ${r.status} · ${r.total_tokens} tokens`
              : 'wf_8K42n · supervisor=gpt-4o · workers=3 · 12.4 秒前启动 · checkpoint #14'}
          </p>
        </div>
        <div className="actions">
          <button className="btn sm">▶ 重放</button>
          <button className="btn sm">✕ 中止</button>
          <button className="btn sm primary">✓ 批准并恢复</button>
        </div>
      </div>
      <div className="tabs">
        <div className="tab active">时间线</div>
        <div className="tab">Agent 图谱</div>
        <div className="tab">工具轨迹</div>
        <div className="tab">记忆</div>
        <div className="tab">状态</div>
        <div className="tab">成本</div>
        <div className="tab">日志</div>
      </div>
    </div>
  )
}

function KpiStrip() {
  return (
    <div className="kpi-strip">
      <div className="kpi">
        <span className="label">成本</span>
        <span className="val">¥0.184</span>
        <span className="delta down">▼ 低于均值 18.4%</span>
      </div>
      <div className="kpi">
        <span className="label">Token 数</span>
        <span className="val">14,892</span>
        <span className="delta">输入 9.1k · 输出 5.8k</span>
      </div>
      <div className="kpi">
        <span className="label">耗时</span>
        <span className="val">
          12.4<span className="u">s</span>
        </span>
        <span className="delta up">▲ 比 p50 快 2.1s</span>
      </div>
      <div className="kpi">
        <span className="label">跳数</span>
        <span className="val">8</span>
        <span className="delta">supervisor=4 worker=4</span>
      </div>
      <div className="kpi">
        <span className="label">LLM 评审</span>
        <span className="val" style={{ color: 'var(--emerald-4)' }}>
          9.1<span className="u">/10</span>
        </span>
        <span className="delta">faith=9.4 · rel=9.0 · hall=0</span>
      </div>
    </div>
  )
}

type GanttLane = {
  agent: string
  initials: string
  color: 'blue' | 'purple' | 'emerald' | 'amber' | 'muted'
  blocks: { left: string; width: string; label: string; running?: boolean }[]
}

const LANES: GanttLane[] = [
  {
    agent: 'supervisor',
    initials: 'SU',
    color: 'blue',
    blocks: [
      { left: '0%', width: '4%', label: 'qualify' },
      { left: '26%', width: '3%', label: 'route' },
      { left: '47%', width: '3%', label: 'route' },
      { left: '71%', width: '3%', label: 'propose' },
    ],
  },
  {
    agent: 'researcher',
    initials: 'RS',
    color: 'purple',
    blocks: [{ left: '5%', width: '21%', label: 'web_search · scrape' }],
  },
  {
    agent: 'analyzer',
    initials: 'AN',
    color: 'emerald',
    blocks: [{ left: '30%', width: '17%', label: 'score · ICP fit' }],
  },
  {
    agent: 'executor',
    initials: 'EX',
    color: 'amber',
    blocks: [{ left: '51%', width: '20%', label: 'draft · CRM stage' }],
  },
  {
    agent: 'human_loop',
    initials: 'HL',
    color: 'muted',
    blocks: [{ left: '74%', width: '24%', label: '等待审批 · 已暂停', running: true }],
  },
]

function GanttChart() {
  return (
    <div className="gantt">
      <div className="head">
        <div className="title">执行时间线 · 逐节点甘特图</div>
        <div className="meta">已用时 12.4s · 预计 t=14.2s</div>
      </div>
      <div className="ruler">
        <div />
        <div className="scale">
          {[0, 1.4, 2.8, 4.2, 5.6, 7.0, 8.4, 9.8, 11.2, 12.6].map((t, i) => (
            <span key={i}>{t}s</span>
          ))}
        </div>
      </div>
      <div className="lanes">
        {LANES.map((lane) => (
          <div className="lane" key={lane.agent}>
            <div className="who">
              <span className="agent-av">
                <span className={`av ${lane.color === 'muted' ? '' : lane.color}`}>
                  {lane.initials}
                </span>
              </span>
              <span>{lane.agent}</span>
            </div>
            <div className="row">
              <div className="grid" />
              {lane.blocks.map((b, i) => (
                <div
                  key={i}
                  className={`block ${lane.color === 'blue' ? '' : lane.color} ${b.running ? 'running' : ''}`}
                  style={{ left: b.left, width: b.width }}
                >
                  {b.label}
                </div>
              ))}
            </div>
          </div>
        ))}
        <div className="now-line" style={{ left: 'calc(140px + (100% - 140px) * 0.88)' }}>
          <span className="label">当前</span>
        </div>
      </div>
    </div>
  )
}

type Ev = { ts: string; src: string; srcClass: 'blue' | 'purple' | 'emerald' | 'amber' | 'red' | 'muted'; msg: React.ReactNode }
const EVENTS: Ev[] = [
  { ts: '+12.41s', src: 'human_loop', srcClass: 'muted', msg: <><span className="lit">paused</span> · 等待 <span className="lit">manager_approval</span> 令牌 <span className="tag">apr_oF92x</span></> },
  { ts: '+12.39s', src: 'executor', srcClass: 'amber', msg: <>工具 <span className="lit">crm.stage</span> 成功 · opportunity_id=<span className="tag">opp_4Lh8q</span> stage=<span className="lit">proposal_draft</span></> },
  { ts: '+11.82s', src: 'executor', srcClass: 'amber', msg: <>draft.email · 1,204 Token · model=<span className="lit">gpt-4o</span> · ¥0.0481</> },
  { ts: '+10.66s', src: 'supervisor', srcClass: 'blue', msg: <>路由 → <span className="lit">executor</span> · “评分 8.4 ≥ 阈值 4.0，进入提案阶段”</> },
  { ts: '+9.94s', src: 'analyzer', srcClass: 'emerald', msg: <>structured.out · score=<span className="lit">8.4</span> · icp_fit=<span className="lit">strong</span> · risks=[<span className="tag">"existing_vendor"</span>]</> },
  { ts: '+7.21s', src: 'analyzer', srcClass: 'emerald', msg: <>memory.recall · 4 条结果 · ns=<span className="tag">sales/stripe</span> · cos≥0.82</> },
  { ts: '+6.84s', src: 'supervisor', srcClass: 'blue', msg: <>路由 → <span className="lit">analyzer</span></> },
  { ts: '+5.71s', src: 'researcher', srcClass: 'purple', msg: <>a2a.send → <span className="lit">analyzer</span> · payload=2.1KB · capability=<span className="tag">score_lead/v1</span></> },
  { ts: '+4.92s', src: 'researcher', srcClass: 'purple', msg: <>工具 <span className="lit">scrape_url</span> · stripe.com/about · 9,841 字符</> },
  { ts: '+2.18s', src: 'researcher', srcClass: 'purple', msg: <>工具 <span className="lit">web_search</span> · q="Stripe Series E 2026" · 8 条结果</> },
  { ts: '+0.92s', src: 'supervisor', srcClass: 'blue', msg: <>路由 → <span className="lit">researcher</span> · “stage=qualify → 收集公司情报”</> },
  { ts: '+0.04s', src: 'checkpointer', srcClass: 'muted', msg: <>检查点 #1 已持久化 · run_id=<span className="tag">wf_8K42n</span></> },
  { ts: '+0.00s', src: 'api', srcClass: 'muted', msg: <><span className="lit">POST /workflows/run</span> · actor=jjt@example.com · role=<span className="tag">sales_rep</span></> },
]

function EventStream() {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">
          <span className="dot live" /> 实时事件流
          <span className="badge mono" style={{ fontSize: 10 }}>
            SSE · /workflows/wf_8K42n/stream
          </span>
        </div>
        <div className="actions">
          <span>筛选：全部</span>
          <span style={{ color: 'var(--fg-faint)' }}>·</span>
          <span>跟随</span>
        </div>
      </div>
      <div className="events">
        {EVENTS.map((e, i) => (
          <div className="event" key={i}>
            <span className="ts">{e.ts}</span>
            <span className={`src ${e.srcClass}`}>{e.src}</span>
            <span className="msg">{e.msg}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

type TraceNode = { depth: number; indent: string; label: string; pct: number; pctColor: string; ms: string }

const TRACE: TraceNode[] = [
  { depth: 0, indent: '▾', label: 'workflow.run', pct: 100, pctColor: 'var(--blue-2)', ms: '12,418 ms' },
  { depth: 1, indent: '▾', label: 'supervisor.route', pct: 4, pctColor: 'var(--blue-3)', ms: '512 ms' },
  { depth: 1, indent: '▾', label: 'researcher.execute', pct: 22, pctColor: 'var(--purple-3)', ms: '2,712 ms' },
  { depth: 2, indent: '├', label: 'tool.web_search', pct: 9, pctColor: 'var(--blue-3)', ms: '1,134 ms' },
  { depth: 2, indent: '├', label: 'tool.scrape_url', pct: 7, pctColor: 'var(--blue-3)', ms: '872 ms' },
  { depth: 2, indent: '└', label: 'a2a.send → analyzer', pct: 0.5, pctColor: 'var(--blue-3)', ms: '63 ms' },
  { depth: 1, indent: '▾', label: 'analyzer.execute', pct: 17, pctColor: 'var(--emerald-3)', ms: '2,118 ms' },
  { depth: 2, indent: '├', label: 'memory.recall · ns=sales/stripe', pct: 3, pctColor: 'var(--blue-3)', ms: '412 ms' },
  { depth: 2, indent: '└', label: 'llm.score · structured(LeadScore)', pct: 14, pctColor: 'var(--blue-3)', ms: '1,706 ms' },
  { depth: 1, indent: '▾', label: 'executor.execute', pct: 24, pctColor: 'var(--amber-2)', ms: '3,012 ms' },
  { depth: 2, indent: '├', label: 'llm.draft_proposal', pct: 18, pctColor: 'var(--blue-3)', ms: '2,318 ms' },
  { depth: 2, indent: '├', label: 'tool.crm.stage', pct: 3, pctColor: 'var(--blue-3)', ms: '381 ms' },
  { depth: 2, indent: '└', label: 'tool.email.compose', pct: 2, pctColor: 'var(--blue-3)', ms: '228 ms' },
  { depth: 1, indent: '▾', label: 'human_loop.await · interrupt_before', pct: 30, pctColor: 'var(--fg-faint)', ms: '~ pending ~' },
]

function ToolTraceTree() {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">工具调用轨迹 · 火焰图</div>
        <div className="actions">
          <span>span 深度：4</span>
        </div>
      </div>
      <div className="trace">
        {TRACE.map((n, i) => (
          <div
            key={i}
            className="node"
            style={{ paddingLeft: n.depth === 0 ? undefined : n.depth === 1 ? 28 : 52 }}
          >
            <span className="indent">{n.indent}</span>
            <span className="label">{n.label}</span>
            <span className="pct">
              <span style={{ width: `${n.pct}%`, background: n.pctColor }} />
            </span>
            <span className="ms">{n.ms}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ApprovalCard() {
  return (
    <div className="approval">
      <div className="hd">
        <span className="badge amber">
          <span className="dot" style={{ background: 'var(--amber-4)' }} /> 需要审批
        </span>
        <span className="meta">
          apr_oF92x · 指派给 <span style={{ color: 'var(--fg-secondary)' }}>@s.chen</span>
        </span>
      </div>
      <div className="ttl">向 Stripe 发送提案 — ¥148K / 12 个月</div>
      <div className="meta">评分 8.4/10 · 符合 ICP · 1 项风险标记</div>
      <pre className="diff" style={{ margin: 0 }}>
        <span className="add">+ stage</span>{'         '}proposal_sent{'\n'}
        <span className="add">+ owner</span>{'         '}s.chen@example.com{'\n'}
        <span className="add">+ amount</span>{'        '}¥148,000 / yr{'\n'}
        <span className="add">+ next_step</span>{'     '}follow_up_2026-06-04{'\n'}
        <span className="rem">- last_touch</span>{'    '}2026-04-12 (qualify)
      </pre>
      <div className="ctas">
        <button className="btn sm primary" style={{ flex: 1, justifyContent: 'center' }}>
          批准 · 恢复
        </button>
        <button className="btn sm" style={{ flex: 1, justifyContent: 'center' }}>
          拒绝
        </button>
      </div>
    </div>
  )
}

function MemoryRecallPanel() {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">记忆召回 · 上下文相关</div>
        <div className="actions">
          <span>ns:sales/stripe</span>
        </div>
      </div>
      <div className="panel-body" style={{ padding: 8 }}>
        <div className="mem-card" style={{ border: 0, padding: '10px 12px' }}>
          <div className="top">
            <span className="ns">决策 · 2025-11-20</span>
            <span className="sim">0.89</span>
          </div>
          <div className="snippet">Stripe 拒绝了 2025 年的扩展合作，理由是现有 Adyen 合同延续至 2026 年 Q2。</div>
        </div>
        <div className="hairline" style={{ margin: '4px 0' }} />
        <div className="mem-card" style={{ border: 0, padding: '10px 12px' }}>
          <div className="top">
            <span className="ns">交互 · 2026-02-08</span>
            <span className="sim">0.84</span>
          </div>
          <div className="snippet">VP Eng 体验了 ForgeFlow，对收入运营自动化表现出兴趣。负责人：s.chen。</div>
        </div>
        <div className="hairline" style={{ margin: '4px 0' }} />
        <div className="mem-card" style={{ border: 0, padding: '10px 12px' }}>
          <div className="top">
            <span className="ns">策略 · global</span>
            <span className="sim">0.78</span>
          </div>
          <div className="snippet">新增 ARR ≥ ¥100K 在发送前需 VP 级审批。</div>
        </div>
      </div>
    </div>
  )
}

function StateDiffPanel() {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">检查点 · #14 → #15 差异</div>
        <div className="actions">
          <span className="mono">postgres://checkpoints</span>
        </div>
      </div>
      <div className="panel-body">
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: '11.5px', lineHeight: 1.7, color: 'var(--fg-secondary)' }}>
          <DiffRow k="stage:" before="qualify" after="propose" />
          <DiffRow k="approval_status:" before="null" after="pending" />
          <DiffRow k="opportunity_id:" before="null" after="opp_4Lh8q" />
          <DiffRow k="analysis_scores[]:" before="" after="+= {score:8.4, icp:strong}" />
          <DiffRow k="tokens_used:" before="0" after="14,892" />
          <DiffRow k="cost_usd:" before="0.0" after="0.184" />
        </div>
      </div>
    </div>
  )
}

function DiffRow({ k, before, after }: { k: string; before: string; after: string }) {
  return (
    <div>
      <span style={{ color: 'var(--fg-muted)' }}>{k}</span>{' '}
      {before && <>{before} </>}
      <span style={{ color: 'var(--blue-4)' }}>→</span> {after}
    </div>
  )
}

function AgentsOnRunPanel() {
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="title">本次运行的 Agent</div>
        <div className="actions">
          <span>4 个活跃 · 1 个已暂停</span>
        </div>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        <table className="tbl">
          <tbody>
            <AgentRow color="blue" initials="SU" name="supervisor" ms="512" suffix="ms" status="空闲" badge="emerald" />
            <AgentRow color="purple" initials="RS" name="researcher" ms="2.7" suffix="s" status="完成" badge="emerald" />
            <AgentRow color="emerald" initials="AN" name="analyzer" ms="2.1" suffix="s" status="完成" badge="emerald" />
            <AgentRow color="amber" initials="EX" name="executor" ms="3.0" suffix="s" status="等待" badge="amber" />
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AgentRow({
  color, initials, name, ms, suffix, status, badge,
}: {
  color: string; initials: string; name: string; ms: string; suffix: string; status: string; badge: string
}) {
  return (
    <tr>
      <td>
        <span className="agent-av">
          <span className={`av ${color}`}>{initials}</span>
          <span className="name">{name}</span>
        </span>
      </td>
      <td className="num">
        {ms}
        <span style={{ color: 'var(--fg-muted)' }}>{suffix}</span>
      </td>
      <td>
        <span className={`badge ${badge}`}>{status}</span>
      </td>
    </tr>
  )
}
