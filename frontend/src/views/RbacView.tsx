// Roster mirrors the backend `ROLE_PERMISSIONS` keys exactly
// (forgeflow/rbac/policies.py) — no more, no less. `anonymous` (the public
// marketplace pseudo-role, no assignable users) is intentionally omitted here,
// as it is in the docs role tables. `count` stays illustrative sample data
// (see the "Sample data" badge); `desc` must reflect each role's real
// permission surface, never an invented one.
const ROLES = [
  {
    name: 'admin',
    desc: 'full platform access (*:*); every resource, every workspace',
    count: 2,
    color: 'red',
  },
  {
    name: 'manager',
    desc: 'execute workflows; approve proposals; read workflows/metrics/agents/memory/audit/leads/workspaces; read+write policies, skills, marketplace; manage self',
    count: 8,
    color: 'amber',
  },
  {
    name: 'sales_rep',
    desc: 'execute workflows; read workflows/metrics/agents/marketplace/skills; read+write memory; manage self',
    count: 24,
    color: 'blue',
  },
  {
    name: 'viewer',
    desc: 'read workflows/metrics/marketplace/skills/policies; manage self (no execute)',
    count: 31,
    color: 'emerald',
  },
  {
    name: 'service',
    desc: 'service-to-service JWT: read+execute workflows, read metrics/skills',
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
            <h1>RBAC &amp; secrets</h1>
            <p className="sub">Role-based access control · JWT auth · scoped API tokens · sample data</p>
          </div>
          <div className="actions">
            <button className="btn sm" disabled title="Policy bundles (OPA-style) are planned, not yet implemented">
              Policy bundle (planned)
            </button>
            <button className="btn sm primary" disabled title="Role management UI is planned; roles are seeded server-side today">
              + Add role
            </button>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="panel">
          <div className="panel-head">
            <div className="title">Roles</div>
            <div className="actions">
              <span className="badge amber" style={{ fontSize: 10 }}>Sample data</span>
              <span>{ROLES.length} defined</span>
            </div>
          </div>
          <div className="panel-body flush">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Description</th>
                  <th className="num">Users</th>
                  <th>Status</th>
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
                      <span className="badge emerald">● active</span>
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
