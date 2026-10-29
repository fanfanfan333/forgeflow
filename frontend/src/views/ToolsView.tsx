import { useQuery } from '@tanstack/react-query'
import { request } from '../api/client'
import { useSession } from '../hooks/useSession'
import { humanizeError } from '../api/errors'

type MCPStatus = {
  status: 'not_checked' | 'connected' | 'unavailable'
  updated_at: string | null
  error: string | null
  tool_count: number
  tools: { id: string; description: string; read_only: boolean }[]
}

export function ToolsView() {
  const session = useSession()
  const status = useQuery({
    queryKey: ['mcp-status', session?.userId, session?.role],
    queryFn: () => request<MCPStatus>('/agents/mcp-status'),
    enabled: !!session,
    refetchInterval: 30_000,
  })
  const labels = { not_checked: '尚未检测', connected: '已连接', unavailable: '暂不可用' }
  return <section className="view active" data-screen-label="工具">
    <div className="page-head"><div className="row"><div>
      <h1>工具</h1>
      <p className="sub">查看当前身份可使用的 MCP 工具。工具调用沿用任务权限、预算和执行记录。</p>
    </div><button className="btn" disabled={status.isFetching || !session} onClick={() => void status.refetch()}>刷新状态</button></div></div>
    <div className="page-body">
      {!session && <p>登录后查看工具。</p>}
      {status.isLoading && <p role="status">正在读取连接状态…</p>}
      {status.isError && <p role="alert">{humanizeError(status.error).label}</p>}
      {status.data && <div className="panel"><div className="panel-body">
        <div className="row"><strong>MCP · {labels[status.data.status]}</strong><span>{status.data.tool_count} 个已授权工具</span></div>
        {status.data.updated_at && <p className="sub">最近检测：{new Date(status.data.updated_at).toLocaleString()}</p>}
        {status.data.status === 'unavailable' && <p>连接或凭证检查未通过，请联系管理员检查配置。</p>}
        {status.data.tools.length === 0 ? <p className="sub">当前没有已连接且获得授权的工具。</p> :
          <table className="tbl"><thead><tr><th>工具</th><th>用途</th><th>范围</th></tr></thead><tbody>
            {status.data.tools.map(tool => <tr key={tool.id}><td className="mono">{tool.id}</td><td>{tool.description}</td><td>只读</td></tr>)}
          </tbody></table>}
      </div></div>}
    </div>
  </section>
}
