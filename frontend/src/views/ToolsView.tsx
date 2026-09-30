/**
 * Tools (MCP) — backend doesn't expose a tool catalog endpoint yet.
 * Static representation of the 4 default tool providers.
 */

const TOOLS = [
  { provider: 'tavily', name: 'web_search', desc: '通过 Tavily 的实时网页搜索', badge: 'blue' },
  { provider: 'internal', name: 'scrape_url', desc: '抓取并解析任意 URL', badge: 'blue' },
  { provider: 'salesforce', name: 'lead.create / .update', desc: '模拟客户关系管理（可替换为真实 SFDC）', badge: 'amber' },
  { provider: 'salesforce', name: 'opportunity.stage', desc: '在阶段之间移动客户关系管理记录', badge: 'amber' },
  { provider: 'email', name: 'compose / send', desc: '通过 SMTP 或厂商 API 起草并发送邮件', badge: 'purple' },
  { provider: 'memory', name: 'recall', desc: '基于 pgvector 的语义召回', badge: 'emerald' },
  { provider: 'memory', name: 'store', desc: '持久化带嵌入向量的记忆', badge: 'emerald' },
  { provider: 'hubspot', name: 'contact.create', desc: 'HubSpot 连接器（需环境变量启用）', badge: 'blue' },
  { provider: 'jira', name: 'issue.create', desc: 'Jira 连接器（需环境变量启用）', badge: 'blue' },
  { provider: 'github', name: 'issue.create', desc: 'GitHub 连接器（需环境变量启用）', badge: 'blue' },
  { provider: 'msgraph', name: 'mail.send', desc: 'Microsoft Graph 连接器', badge: 'blue' },
  { provider: 'servicenow', name: 'incident.create', desc: 'ServiceNow 连接器', badge: 'blue' },
  { provider: 'quickbooks', name: 'invoice.create', desc: 'QuickBooks 连接器', badge: 'amber' },
  { provider: 'sap', name: 'sales_order.create', desc: 'SAP S/4 OData 连接器', badge: 'amber' },
]

export function ToolsView() {
  return (
    <section className="view active" data-screen-label="工具">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>工具 · MCP</h1>
            <p className="sub">
              默认 MCP 工具提供方参考清单，通过 MCP 服务器暴露在 <span className="mono">:8001</span> 端口上。
              可用性取决于你配置的凭据 —— 目前尚无实时工具目录端点。
            </p>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="panel">
          <div className="panel-body flush">
            <table className="tbl">
              <thead>
                <tr>
                  <th>提供方</th>
                  <th>工具</th>
                  <th>描述</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {TOOLS.map((t) => (
                  <tr key={`${t.provider}.${t.name}`}>
                    <td>
                      <span className={`badge ${t.badge}`}>{t.provider}</span>
                    </td>
                    <td className="mono">{t.name}</td>
                    <td style={{ color: 'var(--fg-secondary)' }}>{t.desc}</td>
                    <td>
                      {/(可选|连接器|模拟|可替换)/.test(t.desc) ? (
                        <span className="badge" title="需要凭据 / 配置">○ 可选</span>
                      ) : (
                        <span className="badge emerald">● 默认</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  )
}
