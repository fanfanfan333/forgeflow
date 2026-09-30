import { useState } from 'react'
import { RunSalesOpsDialog } from '../components/RunSalesOpsDialog'

type Status = 'production' | 'scaffold'

type Template = {
  name: string
  title: string
  desc: string
  color: 'blue' | 'emerald' | 'amber'
  status: Status
  ctaPrimary: string
  connector?: string
  missingPieces?: string[]
}

const TEMPLATES: Template[] = [
  {
    name: 'sales_ops',
    title: '销售线索资质评估',
    desc: '资质评估 → 调研 → 分析 → 提案 → 审批 → 执行',
    color: 'blue',
    status: 'production',
    ctaPrimary: '运行 →',
    connector: 'HubSpot CRM · 按邮箱新增或更新 + 幂等成交',
  },
  {
    name: 'support_ops',
    title: '客户支持工单分流',
    desc: '归类 → 排查 → 响应 → 升级 → 解决',
    color: 'emerald',
    status: 'scaffold',
    ctaPrimary: '仅试运行',
    missingPieces: [
      '未接入工单系统连接器（Zendesk / Intercom / Freshdesk）',
      '缺少知识库检索工具',
      '缺少幂等的工单回复路径',
    ],
  },
  {
    name: 'finance_recon',
    title: '财务对账',
    desc: '采集 → 匹配 → 标记差异 → 审批 → 过账',
    color: 'amber',
    status: 'scaffold',
    ctaPrimary: '仅试运行',
    missingPieces: [
      '未接入银行 / ERP 数据源（QuickBooks + SAP 已存在但尚未连接）',
      '缺少带复式记账校验的过账工具',
      '缺少合规审计留痕（期间锁定、签名凭证）',
    ],
  },
]

export function WorkflowsView() {
  const productionCount = TEMPLATES.filter((t) => t.status === 'production').length
  const scaffoldCount = TEMPLATES.filter((t) => t.status === 'scaffold').length
  return (
    <section className="view active" data-screen-label="工作流">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>工作流</h1>
            <p className="sub">
              {productionCount} 个生产可用 · {scaffoldCount} 个模板脚手架 · 通过运行接口（POST /workflows/run）触发
            </p>
          </div>
          <div className="actions">
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/docs/sales-ops-production.md"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm"
            >
              生产运行手册 →
            </a>
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/forgeflow/workflows/sales_ops/pipeline.py"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm primary"
            >
              + 新建（示例）→
            </a>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="grid-3">
          {TEMPLATES.map((t) => (
            <TemplateCard key={t.name} t={t} />
          ))}
        </div>
        <FootnoteBanner />
      </div>
    </section>
  )
}

function TemplateCard({ t }: { t: Template }) {
  const isProduction = t.status === 'production'
  const [runOpen, setRunOpen] = useState(false)
  return (
    <div className="card" style={{ padding: 20, display: 'flex', flexDirection: 'column', minHeight: 280 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <span className={`badge ${t.color}`}>{t.name}</span>
        {isProduction ? (
          <span className="badge emerald">
            <span className="dot live" /> 生产可用
          </span>
        ) : (
          <span className="badge amber">⚠ 模板脚手架</span>
        )}
      </div>
      <h3
        style={{
          margin: '0 0 6px',
          fontSize: 17,
          fontWeight: 500,
          letterSpacing: 'var(--tracking-tight)',
        }}
      >
        {t.title}
      </h3>
      <p style={{ margin: 0, color: 'var(--fg-secondary)', fontSize: 13, lineHeight: 1.5 }}>{t.desc}</p>

      {isProduction && t.connector && (
        <div
          style={{
            marginTop: 14,
            padding: '8px 12px',
            background: 'var(--bg-inset)',
            borderRadius: 6,
            borderLeft: '2px solid var(--emerald-4)',
            fontSize: 12,
            color: 'var(--fg-secondary)',
            fontFamily: 'var(--font-mono)',
          }}
        >
          {t.connector}
        </div>
      )}

      {!isProduction && t.missingPieces && (
        <div
          style={{
            marginTop: 14,
            padding: '10px 12px',
            background: 'var(--bg-inset)',
            borderRadius: 6,
            borderLeft: '2px solid var(--amber-4)',
            fontSize: 12,
            color: 'var(--fg-secondary)',
            lineHeight: 1.5,
          }}
        >
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              letterSpacing: '.12em',
              textTransform: 'uppercase',
              color: 'var(--amber-4)',
              marginBottom: 6,
            }}
          >
            距离生产可用尚缺
          </div>
          {t.missingPieces.map((m) => (
            <div key={m} style={{ paddingLeft: 12, position: 'relative', marginTop: 3 }}>
              <span
                style={{
                  position: 'absolute',
                  left: 0,
                  top: 7,
                  width: 4,
                  height: 4,
                  background: 'var(--fg-faint)',
                  borderRadius: '50%',
                }}
              />
              {m}
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 'auto', paddingTop: 16, display: 'flex', gap: 8 }}>
        {isProduction ? (
          <>
            <button className="btn sm primary" onClick={() => setRunOpen(true)}>
              {t.ctaPrimary}
            </button>
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/docs/sales-ops-production.md"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm"
            >
              运行手册
            </a>
          </>
        ) : (
          <button className="btn sm" disabled title="模板脚手架 —— 请先接入连接器">
            {t.ctaPrimary}
          </button>
        )}
        <a href="/architecture#architecture" className="btn sm">查看流程图</a>
      </div>
      {isProduction && <RunSalesOpsDialog open={runOpen} onClose={() => setRunOpen(false)} />}
    </div>
  )
}

function FootnoteBanner() {
  return (
    <div
      style={{
        marginTop: 20,
        padding: '14px 18px',
        background: 'var(--bg-canvas)',
        border: '1px solid var(--border-subtle)',
        borderLeft: '2px solid var(--blue-4)',
        borderRadius: 'var(--r-3)',
        fontSize: 12.5,
        color: 'var(--fg-secondary)',
        lineHeight: 1.55,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '.12em',
          textTransform: 'uppercase',
          color: 'var(--blue-4)',
          marginBottom: 6,
        }}
      >
        诚实声明
      </div>
      三个具名模板 ≠ 三个生产就绪的工作流。目前只有「销售线索资质评估」工作流具备真实连接器
      （HubSpot）、重试幂等、退避重试、端到端校验脚本以及 Fly.io 部署方案。另外两个只是图 + 提示词脚手架 ——
      从接口调用会抛错，除非开启试运行模式。参考实现见「销售运营生产运行手册」。
    </div>
  )
}
