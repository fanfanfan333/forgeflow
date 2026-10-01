// The in-app /docs route renders the repo's docs/*.md as the single source of
// truth. This manifest defines which pages appear, their order, and grouping.
// `file` is the path under docs/ (e.g. "tutorials/01-first-workflow.md").

export type DocGroup = '入门' | '指南' | '参考' | '运维' | '支持'

export type DocEntry = {
  slug: string
  file: string
  title: string
  group: DocGroup
  summary?: string
}

export const DOC_GROUPS: DocGroup[] = ['入门', '指南', '参考', '运维', '支持']

// Order defines the reading path (prev/next pagination follows it).
export const DOCS: DocEntry[] = [
  // Getting started — the tutorial series, in order.
  { slug: 'tutorials', file: 'tutorials/README.md', title: '教程', group: '入门', summary: '手把手的逐步操作指南。' },
  { slug: 'tutorials-first-workflow', file: 'tutorials/01-first-workflow.md', title: '你的第一个工作流', group: '入门', summary: '启动整套服务，端到端跑通「销售线索资质评估」工作流。' },
  { slug: 'tutorials-ollama', file: 'tutorials/02-run-offline-with-ollama.md', title: '用 Ollama 离线运行', group: '入门', summary: '用本地 LLM 执行工作流。' },
  { slug: 'tutorials-streaming', file: 'tutorials/03-streaming-and-debugging.md', title: '流式查看与调试运行', group: '入门', summary: 'SSE、逐智能体追踪与失败模式。' },
  { slug: 'tutorials-memory', file: 'tutorials/04-semantic-memory.md', title: '语义记忆', group: '入门', summary: '用 pgvector 存储与召回上下文。' },
  { slug: 'tutorials-custom-tool', file: 'tutorials/05-custom-mcp-tool.md', title: '编写自定义工具', group: '入门', summary: '添加一个智能体能自动识别的 MCP 工具。' },

  // Guides — task-oriented, after the basics.
  { slug: 'examples', file: 'examples.md', title: '示例', group: '指南', summary: '可运行的 curl、Python 与流式调用示例。' },
  { slug: 'connectors', file: 'connectors.md', title: '连接器', group: '指南', summary: '企业连接器的凭据与配置。' },
  { slug: 'sales-ops-production', file: 'sales-ops-production.md', title: '销售运营运行手册', group: '指南', summary: '在 Fly.io 上运行真实的 HubSpot 流水线。' },

  // Reference — look-up material.
  { slug: 'api-reference', file: 'api-reference.md', title: 'API 参考', group: '参考', summary: '接口、鉴权、角色与错误语义。' },
  { slug: 'configuration', file: 'configuration.md', title: '配置', group: '参考', summary: '所有环境变量及其默认值。' },
  { slug: 'architecture', file: 'architecture.md', title: '架构', group: '参考', summary: '系统设计与 Mermaid 图示。' },
  { slug: 'database', file: 'database.md', title: '数据库', group: '参考', summary: '数据库结构、ER 图与迁移。' },
  { slug: 'auth', file: 'auth.md', title: '认证', group: '参考', summary: '令牌、刷新轮换、MFA、OIDC、RBAC。' },
  { slug: 'testing', file: 'testing.md', title: '测试', group: '参考', summary: '如何运行与编写测试。' },

  // Operations — running ForgeFlow for real.
  { slug: 'operations-backup-dr', file: 'operations/backup-dr.md', title: '备份与灾难恢复', group: '运维', summary: '备份、恢复、RPO/RTO 与灾难恢复手册。' },
  { slug: 'deployment-airgapped', file: 'deployment/AIRGAPPED.md', title: '离线（气隙）部署', group: '运维', summary: '离线打包工具。' },

  // Support — when something is unclear or broken.
  { slug: 'troubleshooting', file: 'troubleshooting.md', title: '故障排查', group: '支持', summary: '首次运行的常见故障与修复。' },
  { slug: 'faq', file: 'faq.md', title: '常见问题', group: '支持', summary: '常见问题速答：哪些已实现、哪些尚未实现。' },
  { slug: 'glossary', file: 'glossary.md', title: '术语表', group: '支持', summary: '所有术语释义。' },
]

export const DOCS_BY_SLUG: Record<string, DocEntry> = Object.fromEntries(DOCS.map((d) => [d.slug, d]))
export const DOCS_BY_FILE: Record<string, DocEntry> = Object.fromEntries(DOCS.map((d) => [d.file, d]))

export function prevNext(slug: string): { prev?: DocEntry; next?: DocEntry } {
  const i = DOCS.findIndex((d) => d.slug === slug)
  if (i === -1) return {}
  return { prev: DOCS[i - 1], next: DOCS[i + 1] }
}

const GH_BLOB = 'https://github.com/JoelJohnsonThomas/forgeflow/blob/main'
const GH_RAW = 'https://raw.githubusercontent.com/JoelJohnsonThomas/forgeflow/main'
const GH_EDIT = 'https://github.com/JoelJohnsonThomas/forgeflow/edit/main'
const GH_ISSUES = 'https://github.com/JoelJohnsonThomas/forgeflow/issues/new'

/** "Edit this page" target on GitHub for a docs/ file. */
export function editUrl(file: string): string {
  return `${GH_EDIT}/docs/${file}`
}

/** Prefilled GitHub issue for reporting a problem with a docs page. */
export function issueUrl(entry: DocEntry): string {
  const title = encodeURIComponent(`docs: feedback on "${entry.title}" (docs/${entry.file})`)
  return `${GH_ISSUES}?title=${title}&labels=documentation`
}

// Resolve a relative path against a doc file, tracking escapes above docs/.
function resolve(fromFile: string, href: string): { path: string; escaped: boolean } {
  const dir = fromFile.includes('/') ? fromFile.slice(0, fromFile.lastIndexOf('/')).split('/') : []
  const stack = [...dir]
  let up = 0
  for (const seg of href.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') {
      if (stack.length) stack.pop()
      else up++
    } else {
      stack.push(seg)
    }
  }
  return { path: stack.join('/'), escaped: up > 0 }
}

export type ResolvedHref =
  | { kind: 'internal'; to: string; hash?: string }
  | { kind: 'anchor'; hash: string }
  | { kind: 'external'; url: string }

// Turn a markdown link href (as authored in docs/) into an in-app route,
// same-page anchor, or external GitHub link.
export function resolveHref(fromFile: string, href: string): ResolvedHref {
  if (/^https?:\/\//i.test(href) || href.startsWith('mailto:')) return { kind: 'external', url: href }
  if (href.startsWith('#')) return { kind: 'anchor', hash: href }

  const [rawPath, hash] = href.split('#')
  const { path, escaped } = resolve(fromFile, rawPath)

  if (escaped) return { kind: 'external', url: `${GH_BLOB}/${path}` }

  const entry = DOCS_BY_FILE[path]
  if (entry) return { kind: 'internal', to: `/docs/${entry.slug}`, hash: hash ? `#${hash}` : undefined }

  // A docs page that isn't in the in-app manifest (or docs/README.md) → GitHub.
  return { kind: 'external', url: `${GH_BLOB}/docs/${path}` }
}

// Resolve an <img src> in a doc to a GitHub raw URL so assets load without
// bundling binaries into the SPA.
export function resolveImg(fromFile: string, src: string): string {
  if (/^https?:\/\//i.test(src) || src.startsWith('data:')) return src
  const [rawPath] = src.split('#')
  const { path, escaped } = resolve(fromFile, rawPath)
  return escaped ? `${GH_RAW}/${path}` : `${GH_RAW}/docs/${path}`
}

// GitHub-style heading slug for in-page anchors.
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/\s+/g, '-')
}
