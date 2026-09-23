/**
 * Tiny typed fetch wrapper for the ForgeFlow FastAPI backend.
 * In dev, requests go through Vite's proxy (/api → http://localhost:8000).
 * In prod, nginx reverse-proxies /api → http://api:8000 inside docker-compose.
 */

const BASE = '/api'
const TOKEN_KEY = 'forgeflow.jwt'
const USER_KEY = 'forgeflow.user'
const ROLE_KEY = 'forgeflow.role'

/** Fired on window whenever the stored session changes (sign-in/out, 401). */
export const AUTH_CHANGED_EVENT = 'ff-auth-changed'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

// --- token storage ---------------------------------------------------------
// SECURITY_AUDIT.md C-2: nginx no longer injects an admin service token. The
// SPA stores the user's JWT in sessionStorage (cleared on tab close, not
// shared between tabs/origins) and attaches it as Authorization on every
// request. Migrate to HttpOnly+Secure cookies + CSRF token once a real
// session backend lands.

export type Session = { userId: string; role: string }

export function setToken(token: string | null): void {
  if (typeof window === 'undefined') return
  if (token) {
    window.sessionStorage.setItem(TOKEN_KEY, token)
  } else {
    window.sessionStorage.removeItem(TOKEN_KEY)
    window.sessionStorage.removeItem(USER_KEY)
    window.sessionStorage.removeItem(ROLE_KEY)
  }
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
}

export function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return window.sessionStorage.getItem(TOKEN_KEY)
}

/** Who is signed in (display only — authorization is enforced server-side). */
export function getSession(): Session | null {
  if (typeof window === 'undefined') return null
  if (!window.sessionStorage.getItem(TOKEN_KEY)) return null
  return {
    userId: window.sessionStorage.getItem(USER_KEY) ?? 'unknown',
    role: window.sessionStorage.getItem(ROLE_KEY) ?? 'unknown',
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  }
  if (token) headers['authorization'] = `Bearer ${token}`

  const res = await fetch(`${BASE}${path}`, { ...init, headers })
  if (res.status === 401) {
    // Stale or revoked token — clear it so the next interaction re-prompts.
    setToken(null)
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

// --- auth helpers ----------------------------------------------------------

export type LoginPayload = {
  user_id: string
  password: string
  mfa_code?: string
  workspace_id?: string
  ttl_hours?: number
}

export async function login(payload: LoginPayload): Promise<{ access_token: string; role: string }> {
  const res = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
  }
  const data = (await res.json()) as { access_token: string; role: string }
  window.sessionStorage.setItem(USER_KEY, payload.user_id)
  window.sessionStorage.setItem(ROLE_KEY, data.role)
  setToken(data.access_token)
  return data
}

export async function logout(): Promise<void> {
  const token = getToken()
  setToken(null)
  if (!token) return
  await fetch(`${BASE}/auth/logout`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token }),
  }).catch(() => undefined)
}

// ---- Types ----------------------------------------------------------------

export type MetricsSummary = {
  total_runs: number
  terminal_runs: number
  success_rate: number
  avg_latency_ms: number
  avg_cost_usd: number
  total_cost_usd: number
  // Explicit "no real data" signals from the backend so the UI renders — (em
  // dash) instead of a misleading 0 / 0.0% / $0.00. `source` names the data
  // source the KPIs were aggregated from (the live hub run store).
  has_data: boolean
  has_cost: boolean
  source: string
}

export type EvaluationSummary = {
  avg_faithfulness: number
  avg_relevance: number
  avg_coherence: number
  hallucination_rate: number
  sample_count: number
}

// ---- Cost board / savings (INC2, frozen contract) -------------------------
// GET /cost/board and GET /cost/savings. `has_data=false` (or a null figure)
// means "no real number available" — the UI must render 「—」, never 0.

export type CostLevel = 'ok' | 'warn' | 'exceeded'

export type CostBudgetRow = {
  scope: 'tenant' | 'team' | 'task'
  scope_id: string | null
  limit: number | null
  spent: number
  pct: number | null
  level: CostLevel
}

export type CostBoard = {
  has_data: boolean
  currency: string
  tenant_id: string
  total_limit: number | null
  total_spent: number
  total_pct: number | null
  level: CostLevel
  budgets: CostBudgetRow[]
}

export type CostSavings = {
  has_data: boolean
  currency: string
  amount: number | null
  baseline: number | null
  actual: number | null
  multiplier: number
  period: string
}

// ---- SLO summary (INC2-07) ------------------------------------------------
// GET /metrics/slo — three frozen tiers (critical / important / edge).
// `has_data=false` degrades to "无数据" (no attainment, no breach claim).

export type SloTier = {
  tier: string
  availability_target: number
  availability_actual: number | null
  p95_target_ms: number
  p95_actual_ms: number | null
  attainment: number | null
  breaching: boolean
  has_data: boolean
  degrade: string
}

export type SloSummary = {
  tiers: SloTier[]
  source: string
}

export type RecentRun = {
  run_id: string
  thread_id: string
  workflow_type: string
  status: string
  created_at: string | null
  completed_at: string | null
  total_tokens: number
  total_cost_usd: number
}

export type Health = {
  status: string
  database: string
  graph: string
}

export type Approval = {
  token: string
  run_id: string
  workflow_id: string
  // The API serialises the proposal under `payload`; `proposal` is the
  // normalised alias the UI reads (mapped in approvalsPending()).
  payload?: Record<string, unknown>
  proposal: Record<string, unknown>
  status: string
  requested_at: string
  resolved_at: string | null
  resolved_by: string | null
  resolution_note: string | null
  expires_at: string | null
}

export type Agent = {
  agent_id: string
  name: string
  endpoint: string
  capabilities?: string[]
  metadata?: Record<string, unknown>
}

export type MemoryResult = {
  id: string
  content: string
  similarity: number
  namespace: string
  metadata: Record<string, unknown>
}

export type AuditRow = {
  id: number
  timestamp: string | null
  user_id: string | null
  role: string | null
  action: string | null
  resource: string | null
  resource_id: string | null
  outcome: string | null
  request_id: string | null
  metadata: Record<string, unknown>
}

export type AuditSearchResponse = {
  total: number
  items: AuditRow[]
  limit: number
  offset: number
  error?: string
}

export type AuditStats = {
  window_days: number
  total: number
  denied: number
  errors: number
  distinct_users: number
  top_resources: { resource: string; hits: number }[]
  error?: string
}

export type CostByAgentRow = {
  agent: string
  total_cost: number
  total_tokens: number
  runs: number
}

export type CostByWorkflowRow = {
  workflow_type: string
  total_cost: number
  total_tokens: number
  runs: number
}

export type TopRun = {
  run_id: string
  workflow_type: string
  total_cost_usd: number | null
  total_tokens: number | null
  created_at: string | null
}

// Raw per-day rows as the API returns them (metrics_store groups by date).
type RawCostByAgent = { agent: string | null; date?: string; total_cost_usd?: number | null; run_count?: number }
type RawCostByWorkflow = {
  workflow_type: string | null
  date?: string
  total_cost_usd?: number | null
  total_tokens?: number | null
  run_count?: number
}

// Mirrors forgeflow/workflows/sales_ops/models.py::LeadInput.
export type SalesLeadInput = {
  company_name: string
  contact_name?: string
  contact_email?: string
  industry?: 'saas' | 'fintech' | 'healthcare' | 'enterprise' | 'ecommerce' | 'martech' | 'other'
  known_budget_usd?: number
  additional_context?: string
}

export type RunWorkflowResponse = {
  run_id: string
  thread_id: string
  status: string
  message?: string
}

// ---- Endpoints ------------------------------------------------------------

export const api = {
  health: () => request<Health>('/health'),
  metricsSummary: () => request<MetricsSummary>('/metrics/'),
  evaluationSummary: () => request<EvaluationSummary>('/metrics/evaluation'),
  // Three-tier SLO attainment (INC2-07).
  sloSummary: () => request<SloSummary>('/metrics/slo'),
  // Frozen cost contract (INC2): budget board + savings vs. baseline. Both
  // surface `has_data` so the UI can fall back to 「—」 instead of a fake 0.
  costBoard: () => request<CostBoard>('/cost/board'),
  costSavings: () => request<CostSavings>('/cost/savings'),
  recentRuns: (limit = 20) => request<RecentRun[]>(`/metrics/runs?limit=${limit}`),
  // The API returns one row per (agent|workflow, day); the console shows
  // window totals, so aggregate here and map run_count/total_cost_usd onto
  // the row shapes the views render.
  costByAgent: async (days = 7): Promise<CostByAgentRow[]> => {
    const rows = await request<RawCostByAgent[]>(`/metrics/cost?days=${days}`)
    const acc = new Map<string, CostByAgentRow>()
    for (const r of rows) {
      const agent = r.agent ?? 'unknown'
      const cur = acc.get(agent) ?? { agent, total_cost: 0, total_tokens: 0, runs: 0 }
      cur.total_cost += Number(r.total_cost_usd ?? 0)
      cur.runs += Number(r.run_count ?? 0)
      acc.set(agent, cur)
    }
    return [...acc.values()].sort((a, b) => b.total_cost - a.total_cost)
  },
  costByWorkflow: async (days = 7): Promise<CostByWorkflowRow[]> => {
    const rows = await request<RawCostByWorkflow[]>(`/metrics/cost/by_workflow_type?days=${days}`)
    const acc = new Map<string, CostByWorkflowRow>()
    for (const r of rows) {
      const wf = r.workflow_type ?? 'unknown'
      const cur = acc.get(wf) ?? { workflow_type: wf, total_cost: 0, total_tokens: 0, runs: 0 }
      cur.total_cost += Number(r.total_cost_usd ?? 0)
      cur.total_tokens += Number(r.total_tokens ?? 0)
      cur.runs += Number(r.run_count ?? 0)
      acc.set(wf, cur)
    }
    return [...acc.values()].sort((a, b) => b.total_cost - a.total_cost)
  },
  topRuns: (days = 7, limit = 10) =>
    request<TopRun[]>(`/metrics/cost/top_runs?days=${days}&limit=${limit}`),
  approvalsPending: async () => {
    const rows = await request<Approval[]>('/approvals/pending')
    // API sends the proposal under `payload`; normalise to `proposal` so the
    // Approvals view (which reads approval.proposal) renders the details.
    return rows.map((r) => ({ ...r, proposal: r.payload ?? r.proposal }))
  },
  approveApproval: (token: string, note = '') =>
    request<{ status: string; thread_id: string }>(`/approvals/${token}/approve`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
  rejectApproval: (token: string, note = '') =>
    request<{ status: string }>(`/approvals/${token}/reject`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
  // The graph runs synchronously (researcher → analyzer → executor LLM calls),
  // so this request routinely takes 1–2 minutes before responding.
  runSalesOps: (lead: SalesLeadInput) =>
    request<RunWorkflowResponse>('/workflows/run', {
      method: 'POST',
      body: JSON.stringify({ workflow_type: 'sales_ops', lead_data: lead }),
    }),
  agents: () => request<Agent[]>('/agents/'),
  agentsDispatch: () => request<unknown>('/agents/dispatch'),
  memorySearch: (q: string, k = 8, namespace?: string) => {
    const u = new URLSearchParams({ q, k: String(k) })
    if (namespace) u.set('namespace', namespace)
    return request<MemoryResult[]>(`/memory/search?${u}`)
  },
  auditSearch: (params: { limit?: number; offset?: number; action?: string } = {}) => {
    const u = new URLSearchParams()
    if (params.limit) u.set('limit', String(params.limit))
    if (params.offset) u.set('offset', String(params.offset))
    if (params.action) u.set('action', params.action)
    const qs = u.toString()
    return request<AuditSearchResponse>(`/audit/search${qs ? `?${qs}` : ''}`)
  },
  auditStats: (days = 7) => request<AuditStats>(`/audit/stats?days=${days}`),
}

// ---- AgentFlow hub types (docs/sop/02-ARCHITECTURE.md §4.1) ---------------

export type PlatformAgent = {
  agent_id: string
  name: string
  category: string
  description: string
  status: string
  color: string
}

export type RunHandle = {
  run_id: string
  thread_id: string
  status: string
  detail: Record<string, unknown>
}

export type RunStep = { tool?: string; step_type?: string; note?: string; index?: number; status?: string }

export type RunDetail = {
  run_id: string
  thread_id: string
  status: string
  outcome: string
  intent: string
  steps: RunStep[]
  errors: string[]
  created_at: string
  completed_at: string | null
  experience_id: string | null
}

export type RunSummary = {
  run_id: string
  thread_id: string
  status: string
  outcome: string
  intent: string
  title: string
  created_at: string
  completed_at: string | null
  experience_id: string | null
  step_count: number
}

export type RunSummaryList = { total: number; items: RunSummary[] }

export type Experience = {
  id: string
  tenant_id: string | null
  team_id: string | null
  run_id: string
  summary: string
  decisions: Record<string, unknown>[]
  outcome: string
  reusable_steps: Record<string, unknown>[]
  tags: string[]
  memory_ids: string[]
  created_at: string
}

export type ExperienceList = { total: number; items: Experience[] }

export type ExperienceLineage = {
  experience: Experience
  run: Record<string, unknown> | null
  memories: string[]
}

export type MemoryEntry = {
  id: string
  tenant_id: string | null
  scope: string
  content: string
  team_id: string | null
  namespace: string
  metadata: Record<string, unknown>
  created_at: string
}

export type MemoryScopeInfo = {
  scope: string
  label: string
  description: string
  writable_by: string[]
  readable_by: string[]
  promotable: boolean
}

export type Skill = {
  id: string
  tenant_id: string | null
  name: string
  domain: string
  owner: string | null
  description: string
  current_version: string | null
  status: string
  usage_count: number
  featured: boolean
  tags: string[]
  created_at: string
  updated_at: string
}

export type SkillList = { total: number; items: Skill[] }

export type SkillVersion = {
  id: string
  skill_id: string
  semver: string
  spec: Record<string, unknown>
  changelog: string
  eval_score: number | null
  source_experience_ids: string[]
  approved_by: string | null
  created_at: string
}

export type SkillCandidate = {
  id: string
  tenant_id: string | null
  name: string
  domain: string
  experience_ids: string[]
  draft_spec: Record<string, unknown>
  similarity_score: number
  status: string
  created_at: string
}

export type SkillCandidateList = { total: number; items: SkillCandidate[] }

export type SkillEvaluation = {
  id: string
  target_id: string
  dataset: string | null
  metrics: Record<string, unknown>
  verdict: string
  created_at: string
}

export type Policy = {
  id: string
  tenant_id: string | null
  subject: string
  resource: string
  action: string
  condition: Record<string, unknown>
  effect: string
  description: string
  created_at: string
}

export type PolicyList = { total: number; items: Policy[] }

export type PolicyDecision = {
  effect: string
  risk_level: string
  hit_policy_id: string | null
  requires_approval: boolean
  reason: string
  approval_id: string | null
}

export type HubApproval = {
  id: string
  tenant_id: string | null
  run_id: string | null
  risk_level: string
  requested_action: string
  requester: string | null
  approver: string | null
  decision: string | null
  status: string
  note: string
  created_at: string
  resolved_at: string | null
}

export type SecurityOverview = {
  status: string
  isolation_level: string
  dlp_enabled: boolean
  encryption: string
  access_control: string
  blocked_count: number
  pending_approvals: number
  policies: number
}

export type HubApprovalList = { total: number; items: HubApproval[] }

// ---- AgentFlow hub endpoints ----------------------------------------------

export const hubApi = {
  agentCatalog: () => request<PlatformAgent[]>('/agents/catalog'),
  createTask: (intent: string, workflowType = 'generic') =>
    request<RunHandle>('/tasks', {
      method: 'POST',
      body: JSON.stringify({ intent, workflow_type: workflowType }),
    }),
  runDetail: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  recentRuns: (limit = 8) => request<RunSummaryList>(`/runs?limit=${limit}`),
  replanRun: (runId: string, reason = '') =>
    request<RunHandle>(`/runs/${runId}/replan`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  experiences: (params: { outcome?: string; tag?: string; run_id?: string; limit?: number; offset?: number } = {}) => {
    const u = new URLSearchParams()
    if (params.outcome) u.set('outcome', params.outcome)
    if (params.tag) u.set('tag', params.tag)
    if (params.run_id) u.set('run_id', params.run_id)
    if (params.limit) u.set('limit', String(params.limit))
    if (params.offset) u.set('offset', String(params.offset))
    const qs = u.toString()
    return request<ExperienceList>(`/experiences${qs ? `?${qs}` : ''}`)
  },
  experienceLineage: (id: string) => request<ExperienceLineage>(`/experiences/${id}/lineage`),
  skills: (params: { domain?: string; q?: string; featured?: boolean; limit?: number; offset?: number } = {}) => {
    const u = new URLSearchParams()
    if (params.domain) u.set('domain', params.domain)
    if (params.q) u.set('q', params.q)
    if (params.featured) u.set('featured', 'true')
    if (params.limit) u.set('limit', String(params.limit))
    if (params.offset) u.set('offset', String(params.offset))
    const qs = u.toString()
    return request<SkillList>(`/skills${qs ? `?${qs}` : ''}`)
  },
  featuredSkills: (limit = 4) => request<SkillList>(`/skills?featured=true&limit=${limit}`),
  createSkill: (body: { name: string; domain: string; owner?: string; description?: string }) =>
    request<Skill>('/skills', { method: 'POST', body: JSON.stringify(body) }),
  skillVersions: (skillId: string) => request<SkillVersion[]>(`/skills/${skillId}/versions`),
  createSkillVersion: (skillId: string, body: { spec: Record<string, unknown>; changelog?: string; bump?: string }) =>
    request<SkillVersion>(`/skills/${skillId}/versions`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  rollbackSkill: (skillId: string, toVersion: string) =>
    request<Skill>(`/skills/${skillId}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ to_version: toVersion }),
    }),
  candidates: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const u = new URLSearchParams()
    if (params.status) u.set('status', params.status)
    if (params.limit) u.set('limit', String(params.limit))
    if (params.offset) u.set('offset', String(params.offset))
    const qs = u.toString()
    return request<SkillCandidateList>(`/skill-candidates${qs ? `?${qs}` : ''}`)
  },
  compileCandidate: (body: { experience_ids?: string[]; mode?: string } = {}) =>
    request<SkillCandidate>('/skill-candidates', { method: 'POST', body: JSON.stringify(body) }),
  evaluateCandidate: (candidateId: string, dataset?: string) =>
    request<SkillEvaluation>(`/skill-candidates/${candidateId}/evaluate`, {
      method: 'POST',
      body: JSON.stringify({ dataset }),
    }),
  promoteCandidate: (candidateId: string, actor?: string) =>
    request<SkillVersion>(`/skill-candidates/${candidateId}/promote`, {
      method: 'POST',
      body: JSON.stringify({ actor }),
    }),
  policies: (params: { subject?: string; resource?: string } = {}) => {
    const u = new URLSearchParams()
    if (params.subject) u.set('subject', params.subject)
    if (params.resource) u.set('resource', params.resource)
    const qs = u.toString()
    return request<PolicyList>(`/policies${qs ? `?${qs}` : ''}`)
  },
  createPolicy: (body: {
    subject: string
    resource: string
    action: string
    condition?: Record<string, unknown>
    effect: string
    description?: string
  }) => request<Policy>('/policies', { method: 'POST', body: JSON.stringify(body) }),
  evaluatePolicy: (body: { subject: string; resource: string; action: string; context?: Record<string, unknown> }) =>
    request<PolicyDecision>('/policies/evaluate', { method: 'POST', body: JSON.stringify(body) }),
  approvals: (params: { status?: string; risk_level?: string } = {}) => {
    const u = new URLSearchParams()
    if (params.status) u.set('status', params.status)
    if (params.risk_level) u.set('risk_level', params.risk_level)
    const qs = u.toString()
    return request<HubApprovalList>(`/approvals${qs ? `?${qs}` : ''}`)
  },
  decideApproval: (approvalId: string, decision: 'approve' | 'reject', note = '') =>
    request<HubApproval>(`/approvals/${approvalId}/decision`, {
      method: 'POST',
      body: JSON.stringify({ decision, note }),
    }),
  securityOverview: () => request<SecurityOverview>('/security/overview'),
  memoryScopes: () => request<MemoryScopeInfo[]>('/memory/scopes'),
  memoryList: (params: { scope?: string; team_id?: string; limit?: number } = {}) => {
    const u = new URLSearchParams()
    if (params.scope) u.set('scope', params.scope)
    if (params.team_id) u.set('team_id', params.team_id)
    if (params.limit) u.set('limit', String(params.limit))
    const qs = u.toString()
    return request<{ total: number; items: MemoryEntry[] }>(`/memory${qs ? `?${qs}` : ''}`)
  },
  createMemory: (body: { scope: string; content: string; team_id?: string; metadata?: Record<string, unknown> }) =>
    request<MemoryEntry>('/memory', { method: 'POST', body: JSON.stringify(body) }),
}
