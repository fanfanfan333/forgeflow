// Roster mirrors the backend `ROLE_PERMISSIONS` keys exactly
// (forgeflow/rbac/policies.py) — no more, no less. `anonymous` (the public
// marketplace pseudo-role, no assignable users) is intentionally omitted here,
// as it is in the docs role tables. `count` stays illustrative sample data
// (see the "Sample data" badge); `desc` must reflect each role's real
// permission surface, never an invented one.
const ROLES = [
  {
    name: 'admin',
    desc: '平台全量访问（*:*）：所有资源、所有工作区',
    count: 2,
    color: 'red',
  },
  {
    name: 'manager',
    desc: '执行工作流；审批提案；读取 workflows/metrics/agents/memory/audit/leads/workspaces；读写 policies、skills、marketplace；管理自身',
    count: 8,
    color: 'amber',
  },
  {
    name: 'sales_rep',
    desc: '执行工作流；读取 workflows/metrics/agents/marketplace/skills；读写 memory；管理自身',
    count: 24,
    color: 'blue',
  },
  {
    name: 'viewer',
    desc: '读取 workflows/metrics/marketplace/skills/policies；管理自身（不可执行）',
    count: 31,
    color: 'emerald',
  },
  {
    name: 'service',
    desc: '服务间 JWT：读取并执行 workflows，读取 metrics/skills',
    count: 4,
    color: 'purple',
  },
]

export function RbacView() {
  return (
    <section className="view active" data-screen-label="RBAC">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>RBAC 与密钥管理</h1>
            <p className="sub">基于角色的访问控制 · JWT 鉴权 · 作用域 API 令牌 · 示例数据</p>
          </div>
          <div className="actions">
            <button className="btn sm" disabled title="策略包（OPA 风格）已在规划中，尚未实现">
              策略包（规划中）
            </button>
            <button className="btn sm primary" disabled title="角色管理界面已在规划中；当前角色由服务端预置">
              + 新增角色
            </button>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="panel">
          <div className="panel-head">
            <div className="title">角色</div>
            <div className="actions">
              <span className="badge amber" style={{ fontSize: 10 }}>示例数据</span>
              <span>共 {ROLES.length} 个</span>
            </div>
          </div>
          <div className="panel-body flush">
            <table className="tbl">
              <thead>
                <tr>
                  <th>角色</th>
                  <th>描述</th>
                  <th className="num">用户数</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {ROLES.map((r) => (
                  <tr key={r.name}>
                    <td>
                      <span className={`badge ${r.color}`}>{r.name}</span>
                    </td>
                    <td style={{ color: 'var(--fg-secondary)' }}>{r.desc}</td>
                    <td className="num">{r.count}</td>
                    <td>
                      <span className="badge emerald">● 启用</span>
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
