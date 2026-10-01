/**
 * Static port of the Clusters view from the design.
 * The backend doesn't yet expose k8s pod state; this renders deterministic
 * sample data so the design fidelity is preserved.
 */

type Cluster = {
  name: string
  region: string
  pods: number
  warn?: number[]
  fail?: number[]
  idle?: number[]
  cpu: string
  mem: string
  p50: string
  rps: string
  note?: string
  badge: 'emerald' | 'plain' | 'purple'
  badgeLabel: string
}

const CLUSTERS: Cluster[] = [
  { name: '主生产环境', region: 'AWS · k8s 1.30 · 6 节点 · 128 容器组', pods: 128, warn: [14, 61], fail: [119], cpu: '62%', mem: '71%', p50: '142ms', rps: '4.8k', note: '2 个重启中', badge: 'emerald', badgeLabel: '健康' },
  { name: '备生产环境', region: 'AWS · k8s 1.30 · 4 节点 · 84 容器组', pods: 84, warn: [22], cpu: '48%', mem: '54%', p50: '168ms', rps: '2.1k', badge: 'emerald', badgeLabel: '健康' },
  { name: '预发环境', region: 'AWS · k8s 1.30 · 2 节点 · 36 容器组', pods: 36, idle: Array.from({ length: 6 }, (_, i) => 30 + i), cpu: '18%', mem: '22%', p50: '184ms', rps: '120', badge: 'plain', badgeLabel: '预发' },
  { name: '离线专有环境', region: '本地部署 · k8s 1.30 · 2 节点 · 64 容器组 · Ollama', pods: 64, cpu: '74%', mem: '81%', p50: '412ms', rps: '820', note: '已离线 14 天', badge: 'purple', badgeLabel: '离线（气隙）' },
]

export function ClustersView() {
  return (
    <section className="view active" data-screen-label="集群">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>集群与部署</h1>
            <p className="sub">4 个环境 · 312 个容器组 · 14 个节点 · 2 个区域 · 示例数据</p>
          </div>
          <div className="actions">
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/tree/main/helm"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm"
            >
              查看 Helm 部署包 →
            </a>
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/releases"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm"
            >
              发布记录 →
            </a>
            <a
              href="https://github.com/JoelJohnsonThomas/forgeflow/blob/main/docs/sales-ops-production.md#deploy-to-flyio-15-min"
              target="_blank"
              rel="noopener noreferrer"
              className="btn sm primary"
            >
              + 新建部署（文档）→
            </a>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="grid-2">
          {CLUSTERS.map((c) => (
            <ClusterCard key={c.name} cluster={c} />
          ))}
        </div>
        <DeploysTable />
      </div>
    </section>
  )
}

type Deploy = {
  version: string
  cluster: string
  author: string
  strategy: string
  status: 'healthy' | 'air-gapped' | 'rolled-back'
  statusLabel: string
  duration: string
  when: string
}

const DEPLOYS: Deploy[] = [
  { version: 'v3.4.1', cluster: '主生产环境', author: '张凯', strategy: '金丝雀 10→100', status: 'healthy', statusLabel: '● 健康', duration: '12m 41s', when: '2 小时前' },
  { version: 'v3.4.1', cluster: '备生产环境', author: '张凯', strategy: '蓝绿发布', status: 'healthy', statusLabel: '● 健康', duration: '9m 12s', when: '2 小时前' },
  { version: 'v3.4.1', cluster: '预发环境', author: '张凯', strategy: '滚动发布', status: 'healthy', statusLabel: '● 健康', duration: '3m 21s', when: '3 小时前' },
  { version: 'v3.4.0', cluster: '离线专有环境', author: '离线包', strategy: '签名离线包', status: 'air-gapped', statusLabel: '● 离线（气隙）', duration: '—', when: '14 天前' },
  { version: 'v3.3.9', cluster: '主生产环境', author: '张凯', strategy: '金丝雀 10→25', status: 'rolled-back', statusLabel: '● 已回滚 · p99 尖刺', duration: '4m 02s', when: '5 天前' },
]

function deployBadge(d: Deploy) {
  if (d.status === 'healthy') return <span className="badge emerald">{d.statusLabel}</span>
  if (d.status === 'air-gapped') return <span className="badge purple">{d.statusLabel}</span>
  return <span className="badge red">{d.statusLabel}</span>
}

function DeploysTable() {
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <div className="title">近期部署</div>
        <div className="actions">
          <span>已启用自动回滚</span>
        </div>
      </div>
      <div className="panel-body flush">
        <table className="tbl">
          <thead>
            <tr>
              <th>版本</th>
              <th>集群</th>
              <th>作者</th>
              <th>策略</th>
              <th>状态</th>
              <th className="num">耗时</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {DEPLOYS.map((d, i) => (
              <tr key={`${d.version}-${d.cluster}-${i}`}>
                <td className="mono">{d.version}</td>
                <td>{d.cluster}</td>
                <td>{d.author}</td>
                <td>{d.strategy}</td>
                <td>{deployBadge(d)}</td>
                <td className="num">{d.duration}</td>
                <td className="text-muted text-mono">{d.when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ClusterCard({ cluster }: { cluster: Cluster }) {
  const warnSet = new Set(cluster.warn ?? [])
  const failSet = new Set(cluster.fail ?? [])
  const idleSet = new Set(cluster.idle ?? [])
  const badgeClass =
    cluster.badge === 'emerald'
      ? 'badge emerald'
      : cluster.badge === 'purple'
      ? 'badge purple'
      : 'badge'
  return (
    <div className="cluster">
      <div className="top">
        <div>
          <div className="name">{cluster.name}</div>
          <div className="region">{cluster.region}</div>
        </div>
        <span className={badgeClass}>
          {cluster.badge === 'emerald' && <span className="dot live" />} {cluster.badgeLabel}
        </span>
      </div>
      <div className="pods">
        {Array.from({ length: cluster.pods }, (_, i) => {
          let cls = 'pod'
          if (failSet.has(i)) cls += ' fail'
          else if (warnSet.has(i)) cls += ' warn'
          else if (idleSet.has(i)) cls += ' idle'
          return <div key={i} className={cls} />
        })}
      </div>
      <div className="stats">
        <span>
          CPU <b>{cluster.cpu}</b>
        </span>
        <span>
          内存 <b>{cluster.mem}</b>
        </span>
        <span>
          P50 延迟 <b>{cluster.p50}</b>
        </span>
        <span>
          每秒请求 <b>{cluster.rps}</b>
        </span>
        {/* 文字用 --amber-fg（--amber-4 在浅色底仅 1.75:1）。 */}
      {cluster.note && <span style={{ color: 'var(--amber-fg)' }}>{cluster.note}</span>}
      </div>
    </div>
  )
}
