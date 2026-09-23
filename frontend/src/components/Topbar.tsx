import { useMatchRoute } from '@tanstack/react-router'
import { IconBell, IconChevronDown, IconHelp, IconSearch } from './icons'
import { AuthControls } from './AuthControls'
import { useSession } from '../hooks/useSession'
import { useHubApprovals } from '../api/hooks'

// Map console routes → breadcrumb segments (last = current page).
const CRUMBS: { match: string; segments: string[] }[] = [
  { match: '/tasks', segments: ['控制台', '智能任务'] },
  { match: '/approvals', segments: ['控制台', '智能任务', '审批'] },
  { match: '/runs', segments: ['控制台', '智能任务'] },
  { match: '/workflows', segments: ['控制台', '智能任务', '工作流'] },
  { match: '/agents', segments: ['控制台', 'Agent 工作台'] },
  { match: '/tools', segments: ['控制台', 'Agent 工作台', '工具'] },
  { match: '/skills', segments: ['控制台', '技能中心'] },
  { match: '/marketplace', segments: ['控制台', '技能中心', '市场'] },
  { match: '/knowledge', segments: ['控制台', '知识库'] },
  { match: '/memory', segments: ['控制台', '记忆管理'] },
  { match: '/analytics', segments: ['控制台', '数据分析'] },
  { match: '/cost', segments: ['控制台', '数据分析', '成本'] },
  { match: '/security', segments: ['控制台', '安全与权限'] },
  { match: '/rbac', segments: ['控制台', '安全与权限', 'RBAC'] },
  { match: '/ops', segments: ['控制台', '运维监控'] },
  { match: '/clusters', segments: ['控制台', '运维监控', '集群'] },
  { match: '/audit', segments: ['控制台', '运维监控', '审计日志'] },
  { match: '/settings', segments: ['控制台', '系统设置'] },
  { match: '/overview', segments: ['控制台', '首页'] },
  { match: '/', segments: ['控制台', '首页'] },
]

function useCrumbs(): string[] {
  const match = useMatchRoute()
  for (const c of CRUMBS) {
    if (match({ to: c.match, fuzzy: false })) return c.segments
  }
  return ['控制台']
}

export function Topbar() {
  const crumbs = useCrumbs()
  const session = useSession()
  const approvals = useHubApprovals({ status: 'pending' })
  const pending = approvals.data?.total ?? 0

  // Workspace label is derived from the live session — no fabricated
  // tenant/region placeholder (QA V9). Real multi-tenant switching
  // lands when /workspaces is wired in.
  const workspaceLabel = session ? `${session.userId} 的工作区` : '未登录'
  const workspaceEnv = session?.role ?? 'anonymous'

  return (
    <header className="topbar">
      <a href="/" className="brand" aria-label="AgentFlow 首页">
        <span className="brand-mark" />
        <span className="brand-name">AgentFlow</span>
      </a>
      <div className="org-pill" title="当前工作区（多租户切换待接入 /workspaces）">
        <span className="logo" />
        <span>{workspaceLabel}</span>
        <span className="env">{workspaceEnv}</span>
        <IconChevronDown style={{ color: 'var(--fg-muted)', marginLeft: 2 }} />
      </div>
      <nav className="crumbs" aria-label="面包屑">
        {crumbs.map((seg, i) => {
          const isLast = i === crumbs.length - 1
          return (
            <span key={`${seg}-${i}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              {i > 0 && <span className="sep">/</span>}
              <span className={isLast ? 'cur' : undefined}>{seg}</span>
            </span>
          )
        })}
      </nav>
      <a
        href="/ops"
        className="search"
        title="搜索任务、Agent、技能、知识库"
        style={{ textDecoration: 'none' }}
      >
        <IconSearch />
        <span className="placeholder">搜索任务、Agent、技能、知识库…</span>
        <span className="kbd">⌘K</span>
      </a>
      <div className="right">
        <a
          href="/api/health"
          target="_blank"
          rel="noopener noreferrer"
          className="status-bar"
          title="API 健康检查"
          style={{ textDecoration: 'none' }}
        >
          <span className="dot live" /> 运行中 · /api/health
        </a>
        <a
          href="/approvals"
          className="btn ghost icon-only"
          title={pending > 0 ? `待处理审批 ${pending}` : '待处理审批'}
          aria-label={`待处理审批 ${pending}`}
          style={{ position: 'relative' }}
        >
          <IconBell />
          {pending > 0 && (
            <span
              aria-hidden="true"
              className="bell-dot"
              style={{
                position: 'absolute',
                top: 5,
                right: 5,
                minWidth: 15,
                height: 15,
                padding: '0 3px',
                borderRadius: 999,
                background: 'var(--red-4)',
                color: 'var(--bg-1, #0b0d12)',
                fontSize: 10,
                fontWeight: 700,
                lineHeight: '15px',
                textAlign: 'center',
              }}
            >
              {pending > 9 ? '9+' : pending}
            </span>
          )}
        </a>
        <a
          href="/docs"
          className="btn ghost icon-only"
          title="文档"
          aria-label="打开文档"
        >
          <IconHelp />
        </a>
        <AuthControls />
      </div>
    </header>
  )
}
