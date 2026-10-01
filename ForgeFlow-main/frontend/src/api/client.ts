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
    // Display-only fallbacks. `role` is still an RBAC enum elsewhere (see
    // Topbar's `?? 'anonymous'`), but here it is only shown, never compared.
    userId: window.sessionStorage.getItem(USER_KEY) ?? '未知',
    role: window.sessionStorage.getItem(ROLE_KEY) ?? '未知',
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
  // Registry liveness signal (forgeflow/a2a/registry.py::all_agents).
  // TRI-STATE, and the distinction carries meaning:
  //   null  → the agent has never emitted a heartbeat, so liveness is UNKNOWN.
  //           Render 「—」, never a fault badge: the four `internal://` cards
  //           registered at boot never call heartbeat().
  //   true  → heartbeated within 60s.
  //   false → last heartbeat is stale. In this codebase heartbeat() only fires
  //           on dispatch, so this means "no recent activity", not "broken".
  // Absent on payloads that predate the field, so the UI must degrade to 「—」.
  healthy?: boolean | null
  last_heartbeat_seconds_ago?: number | null
  runs_completed?: number
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
      const agent = r.agent ?? '未知'
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
      const wf = r.workflow_type ?? '未知'
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
  // INC32 / T05 (additive) — the workspace relationships (ADR-02). `session_id`
  // groups the run into a conversation; `parent_run_id` records the Follow-up
  // chain (AC-39). Both optional so a pre-INC32 response still types.
  session_id?: string
  parent_run_id?: string | null
}

export type RunStep = {
  tool?: string
  step_type?: string
  note?: string
  index?: number
  /**
   * INC12 A1 / INC15 status contract, decided by the handler's *real* behaviour
   * — not a per-step assertion that the plan succeeded:
   *   ok          → executed, succeeded
   *   error       → executed, failed
   *   unavailable → no binding for the tool
   *   refused     → dev-only tool in a non-dev environment
   *   blocked     → needed but a required input is missing (not executed, and
   *                 *not* a failure). INC15 retires the old `skipped` writer
   *                 status; a legacy payload still renders it via the reader's
   *                 `skipped → blocked` alias.
   */
  status?: string
  step_id?: string
  /** `"required"` / `"blocked"` — the L1 plan step's applicability. */
  applicability?: string
  /**
   * INC22 W3.2 / INC23 — the step's blocked reason, when the producer wrote one.
   * `steps[].blocked_reason` and `plan.steps[].blocked_reason` now carry the
   * **same** value: `planning.py::PlanStep.to_payload` and `to_dict` both emit it
   * (`to_payload` was made inclusive in INC23, and `plan_from_records` stopped
   * leaving it empty), so there is no longer a single "authoritative copy" — the
   * two agree. Rendered **verbatim** and never invented when absent (the UI then
   * shows the label alone).
   */
  blocked_reason?: string
}

/** A real tool invocation recorded by the runtime (INC12 A1 / INC15). */
export type RunToolInvocation = {
  tool?: string
  status?: string
  /** True only when the handler actually ran and produced a real outcome. */
  executed?: boolean
  /**
   * INC15 — whether the handler was actually *invoked*. Distinct from `executed`
   * (which means "produced a real outcome"): `ok`/`error` are invoked, while
   * `unavailable`/`refused`/`blocked` are not.
   */
  invoked?: boolean
  /**
   * INC15 — the real per-tool duration in milliseconds, or `null` when it was
   * never measured (blocked / unavailable / refused). Sub-millisecond values
   * keep their precision (e.g. `0.062`); the UI renders 「—」 for `null` and the
   * number for a real measurement — never a fabricated `0`.
   */
  latency_ms?: number | null
  /** `"{run_id}:{attempt}:{index}"` — unique per replan attempt. */
  step_id?: string
  note?: string
  [k: string]: unknown
}

/**
 * One run deliverable (任务产物), verbatim from `GET /runs/{id}`.artifacts (INC14).
 *
 * `content` is the handler's raw return — the UI renders it **verbatim** and
 * must never reformat / translate / template it (honesty rule P0-2). When a run
 * produced no deliverable the list is simply empty and the page shows its
 * honest empty state rather than inventing a result.
 */
export type RunArtifact = {
  id: string
  /** "report_markdown" today. */
  kind: string
  /** "运行报告". */
  title: string
  /** "markdown". */
  format: string
  /** The real Markdown body — rendered verbatim. */
  content: string
  /** The tool that produced it ("report.render"). */
  source: string
  /** sha256(content)[:32] — the artifact ⇄ evidence join. */
  result_ref: string
  created_at: string
}

/**
 * One planned step of a run's **Task Plan** (L1, INC15). The plan is a pure
 * function of the task's real signals, so it lists only the steps that applied
 * plus any *declared* step that is `blocked` (kept visible with a reason) — an
 * irrelevant candidate never reaches `plan.steps` (it goes to `not_applicable`).
 */
export type RunPlanStep = {
  step_id?: string
  index?: number
  tool?: string
  note?: string
  step_type?: string
  /** `"required"` (has its input) or `"blocked"` (declared, input missing). */
  applicability?: string
  required_inputs?: string[]
  blocked_reason?: string
}

/** A candidate step that does not apply to this task (L1 only — never run). */
export type RunPlanNotApplicable = {
  tool?: string
  reason?: string
}

/** The run's **Task Plan** (L1, INC15), verbatim from `GET /runs/{id}`.plan. */
export type RunPlan = {
  run_id?: string
  attempt?: number
  source?: string
  reasoning?: string
  steps?: RunPlanStep[]
  not_applicable?: RunPlanNotApplicable[]
  summary?: {
    planned?: number
    executed?: number
    succeeded?: number
    blocked?: number
    failed?: number
    not_applicable?: number
  }
}

/**
 * One round of the ReAct closed loop (INC17), from
 * `GET /runs/{id}`.llm.rounds. One round == one model-issued tool call, with the
 * platform's own view of it: the (model-supplied) args, the honest status, and a
 * bounded result snippet. This is **non-four-layer** data — the four-layer
 * contract (plan / tool_invocations / observations / artifacts) is untouched.
 */
export type RunRound = {
  /** Which model call this round belongs to (0-based). */
  iteration?: number
  tool?: string
  /** The args the model supplied for the call (shown key/value in the card). */
  args?: Record<string, unknown>
  arguments_hash?: string
  status?: string
  executed?: boolean
  invoked?: boolean
  latency_ms?: number | null
  result_ref?: string | null
  /** ≤200-char snippet of the recorded payload (the single truncation point). */
  result_snippet?: string
  /** The model's text on the round that issued the call. */
  model_text?: string
}

/** The model's own final answer (INC17) — present only on model convergence. */
export type RunFinalAnswer = {
  text?: string
  iteration?: number
  terminated_by?: string
}

/**
 * Executor provenance (`GET /runs/{id}`.llm — the backend's free-form
 * `RunRecord.llm`). INC17 adds `rounds` / `final_answer` / `terminated_by`
 * (additive); every field is optional so a pre-INC17 payload still types.
 */
export type RunLLM = {
  runtime_mode?: string
  /** The per-round trace of the ReAct loop (absent on pre-INC17 runs). */
  rounds?: RunRound[]
  /** The model's final answer, or `null` when the loop was cut off / halted. */
  final_answer?: RunFinalAnswer | null
  /** `"model"` | `"max_iterations"` | `"halted"` (mutually exclusive). */
  terminated_by?: string
  [k: string]: unknown
}

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
  // Runtime truth for the run. The backend has returned all of these since
  // INC4/INC12; they were simply missing from this type, which is part of why
  // the console rendered a demo run instead of the real one.
  //
  // INC20 / T01 — `number | null`. The backend now passes `null` through when the
  // usage was never measured (pre-INC4 records) instead of coercing it to `0`;
  // the type must be equally honest. Same口径 as `RunRound.latency_ms`
  // (`number | null`, below). NEVER read `typeof x === 'number'` as "已测量" —
  // see `roles.isModelDriven`.
  total_tokens?: number | null
  total_cost_usd?: number | null
  runtime_mode?: string
  llm?: RunLLM
  loop?: Record<string, unknown>
  tool_invocations?: RunToolInvocation[]
  actor_user_id?: string
  actor_role?: string
  tenant_id?: string | null
  // INC14 — the run's deliverables (任务产物), verbatim from the backend. Absent
  // on pre-INC14 payloads, so always read it through `deriveArtifacts()` which
  // degrades to `[]` rather than inventing anything.
  artifacts?: RunArtifact[]
  // INC15 — the run's **Task Plan** (L1) and its **Observations** (L3, only the
  // steps that really executed). Both are absent on pre-INC15 payloads, so read
  // them through the `derive*` helpers which degrade to `{}/[]` rather than
  // inventing a plan or padding with fake observations.
  plan?: RunPlan
  observations?: RunToolInvocation[]
  // INC25 / T05 — 代码执行面（codeplane）的**并行**字段（与 `llm` 平级、互不污染）。
  // 后端 `RunRecord.codeplane` 经 `RunDetailResponse.codeplane`（additive，默认 `{}`）
  // 透传；老记录降级为 `{}`（`deriveCodePlane` 据此判「非代码任务」）。读取一律经
  // `realRun.ts::deriveCodePlane`，不在组件里直接摸原始字典。
  codeplane?: Record<string, unknown>
  // INC32 / T05 (additive) — the workspace relationships (ADR-02). Absent on a
  // pre-INC32 payload, so read through `?? ''` — never invented. `session_id`
  // groups the run into a conversation; `parent_run_id` records the Follow-up
  // chain (AC-39).
  session_id?: string
  parent_run_id?: string | null
  // INC33 (additive) — whether this run's execution detail (steps / tool calls /
  // timeline) survived into the current process. Absent on a pre-INC33 payload
  // ⇒ read through `!== false` (default: the detail is here). `false` means the
  // run was hydrated from the persisted header after a restart and the UI must
  // say so, not render an empty step list as if nothing happened.
  detail_retained?: boolean
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
  /**
   * 步数。`GET /runs` 返回的 run 恒为真实计数；由**持久会话摘要**映射来的行
   * （`views/runs/history.ts::sessionToRunSummary`，源 `GET /workspace/sessions`）
   * 不携带步数 ⇒ 字段**缺席**（可选）。渲染方必须按「缺席 = 不展示」处理，
   * 绝不填 0 伪造。
   */
  step_count?: number
  // INC32 / T05 (additive) — the workspace relationships (see `RunDetail`).
  session_id?: string
  parent_run_id?: string | null
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
  /**
   * INC34 — the **configured** minimum number of similar experiences the
   * compiler requires (backend `SKILL_CANDIDATE_MIN_EXPERIENCES`). Only present
   * when `status === 'insufficient'`, so the UI can state the honest
   * 「需要至少 N 条」 without inventing the number. `null` / absent otherwise.
   */
  required_experiences?: number | null
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

// ---- 技能市场（skill listings + 工作流模板）---------------------------------
// 全部来自真实后端：GET /marketplace/skills、POST /marketplace/skills/publish、
// POST /marketplace/skills/{id}/install、POST /marketplace/skills/{id}/rate、
// GET /marketplace/templates、POST /marketplace/templates/refresh。前端不编造。

export type MarketplaceListing = {
  id: string
  tenant_id: string | null
  skill_id: string
  version: string
  name: string
  domain: string
  description: string
  /** 是否跨租户可见（默认 false，见 marketplace_bridge §7.3）。 */
  shared: boolean
  listed_by: string | null
  rating: number
  rating_count: number
  installs: number
  created_at: string
}

export type MarketplaceListingList = { total: number; items: MarketplaceListing[] }

export type MarketplaceTemplate = {
  name: string
  version: string
  description: string
  domain: string
  author?: string
  homepage?: string
  tags?: string[]
  stages?: unknown[]
  requires_connectors?: string[]
  requires_extras?: string[]
  license?: string
}

export type MarketplaceTemplateList = { total: number; templates: MarketplaceTemplate[] }

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

// ---- Resource Center (INC25 W1) -------------------------------------------
// 五类资源（file / database / git_repo / knowledge_base / api）共用一份响应形状；
// 只有 `locator` 随类型不同。`summary` 里的数字**未测量即为 `null`**（绝不编造 0），
// `keywords` **无真实来源时后端直接省略该键**。前端只呈现，不加工。

export type ResourceSummary = {
  kind?: string
  rows?: number | null
  columns?: number | null
  fields?: string[]
  chars?: number | null
  pages?: number | null
  /** 仅当后端确有真实关键词来源时才存在该键（否则整个键缺席）。 */
  keywords?: string[]
  quality?: Record<string, unknown>
  /** `true` ⇒ 摘要来自离线占位能力（如离线档数据库），界面须如实标注。 */
  stub?: boolean
  note?: string
}

export type ResourceRecord = {
  id: string
  tenant_id: string | null
  /** `file | database | git_repo | knowledge_base | api`。 */
  kind: string
  name: string
  created_by: string
  created_at: string
  /** `registered | parsed | metadata_only | ignored | unavailable`。 */
  status: string
  detail: string
  summary: ResourceSummary
  locator: Record<string, unknown>
  /** 仅当内容真的被解析（`status === 'parsed'`）时为 true。 */
  parsed: boolean
}

export type ResourceListResponse = { total: number; items: ResourceRecord[] }

/**
 * INC26 T01 —— 上传上限 + 类型白名单（P0-2）。
 *
 * 值来自后端**单一事实源**（`Settings.multimodal_max_bytes` 与
 * `summaries.SUPPORTED_FILE_EXTENSIONS` 的运行时读取）。前端**不得**写死这两个数字/
 * 列表（有一条钉子会静态扫描本目录），只做展示与上传前预检。
 */
export type ResourceLimitsResponse = {
  max_bytes: number
  supported_extensions: string[]
  /** 后端给出的诚实说明（provenance）。 */
  note?: string
}

/**
 * INC26 T03 —— 资源内容预览（首 N 行 / 行）。与后端
 * `api/resource_schemas.py::ResourcePreviewResponse` **逐字段对齐**。
 * `available=false` / `format='none'` 驱动诚实空态；`truncated` 驱动截断提示。
 */
export type ResourcePreviewResponse = {
  id: string
  kind?: string
  available: boolean
  format: 'table' | 'text' | 'none' | string
  columns: string[]
  rows: string[][]
  content: string
  truncated: boolean
  note: string
}

/** 取回上传上限 + 类型白名单（`GET /resources/limits`，后端单一事实源）。 */
export async function fetchResourceLimits(): Promise<ResourceLimitsResponse> {
  return request<ResourceLimitsResponse>('/resources/limits')
}

/** 取一个资源的内容预览（`GET /resources/{id}/preview?n=`）。 */
export async function previewResource(id: string, n = 20): Promise<ResourcePreviewResponse> {
  return request<ResourcePreviewResponse>(
    `/resources/${encodeURIComponent(id)}/preview?n=${encodeURIComponent(String(n))}`,
  )
}

/** 一次资源登记请求（五类各自独立；字段与后端请求体逐字一致）。 */
export type RegisterResourceInput =
  | { kind: 'file'; file: File }
  | { kind: 'database'; table: string; name?: string }
  | { kind: 'git_repo'; source_type: string; identifier: string; branch?: string }
  | { kind: 'knowledge_base'; kb_id: string; scope?: string }
  | { kind: 'api'; connector: string; base_url?: string }

/**
 * Register one resource against the real `/resources*` endpoints.
 *
 * The file path is multipart (`POST /resources/files`, field name `file`), so it
 * is a dedicated `fetch` rather than the JSON `request` helper. Every failure is
 * surfaced verbatim through `ApiError` (413 over-limit / 400 unsupported type /
 * any other status) — never swallowed, never reported as success.
 */
export async function registerResource(input: RegisterResourceInput): Promise<ResourceRecord> {
  if (input.kind === 'file') {
    const token = getToken()
    const form = new FormData()
    form.append('file', input.file)
    const res = await fetch(`${BASE}/resources/files`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: form,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
    }
    return res.json() as Promise<ResourceRecord>
  }
  if (input.kind === 'database') {
    return request<ResourceRecord>('/resources/database', {
      method: 'POST',
      body: JSON.stringify({ table: input.table, name: input.name ?? '' }),
    })
  }
  if (input.kind === 'git_repo') {
    return request<ResourceRecord>('/resources/code', {
      method: 'POST',
      body: JSON.stringify({
        source_type: input.source_type,
        identifier: input.identifier,
        branch: input.branch ?? '',
      }),
    })
  }
  if (input.kind === 'knowledge_base') {
    return request<ResourceRecord>('/resources/knowledge_base', {
      method: 'POST',
      body: JSON.stringify({ kb_id: input.kb_id, scope: input.scope ?? '' }),
    })
  }
  return request<ResourceRecord>('/resources/api', {
    method: 'POST',
    body: JSON.stringify({ connector: input.connector, base_url: input.base_url ?? '' }),
  })
}

// ---- AgentFlow hub endpoints ----------------------------------------------

export const hubApi = {
  agentCatalog: () => request<PlatformAgent[]>('/agents/catalog'),
  // INC14 — `context` is optional; when omitted the request body is byte-for-byte
  // what it always was (`{ intent, workflow_type }`). "继续执行" passes
  // `{ continued_from_run_id, continued_from_artifact_ref }` so the previous run
  // id really reaches the runtime instead of being dropped at the route.
  createTask: (intent: string, workflowType = 'generic', context?: Record<string, unknown>) =>
    request<RunHandle>('/tasks', {
      method: 'POST',
      body: JSON.stringify({ intent, workflow_type: workflowType, ...(context ? { context } : {}) }),
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
  // ---- 技能市场（真实接口）--------------------------------------------------
  marketplaceListings: (
    params: { q?: string; domain?: string; cross_tenant?: boolean; limit?: number } = {},
  ) => {
    const u = new URLSearchParams()
    if (params.q) u.set('q', params.q)
    if (params.domain) u.set('domain', params.domain)
    if (params.cross_tenant) u.set('cross_tenant', 'true')
    if (params.limit) u.set('limit', String(params.limit))
    const qs = u.toString()
    return request<MarketplaceListingList>(`/marketplace/skills${qs ? `?${qs}` : ''}`)
  },
  publishListing: (body: {
    skill_id: string
    shared?: boolean
    description?: string
    version?: string
  }) =>
    request<{ published: boolean; listing: MarketplaceListing }>('/marketplace/skills/publish', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  installListing: (listingId: string) =>
    request<{ installed: boolean; listing: MarketplaceListing; installed_by: string }>(
      `/marketplace/skills/${encodeURIComponent(listingId)}/install`,
      { method: 'POST' },
    ),
  rateListing: (listingId: string, body: { score: number; comment?: string }) =>
    request<{ rated: boolean; listing: MarketplaceListing }>(
      `/marketplace/skills/${encodeURIComponent(listingId)}/rate`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  marketplaceTemplates: (params: { domain?: string; tag?: string } = {}) => {
    const u = new URLSearchParams()
    if (params.domain) u.set('domain', params.domain)
    if (params.tag) u.set('tag', params.tag)
    const qs = u.toString()
    return request<MarketplaceTemplateList>(`/marketplace/templates${qs ? `?${qs}` : ''}`)
  },
  refreshTemplates: () =>
    request<{ refreshed: boolean; total: number }>('/marketplace/templates/refresh', {
      method: 'POST',
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
  // INC25 / T05 —— 资源清单（`GET /resources`，可选 `kind` 过滤）。登记入口见
  // 独立导出的 `registerResource`（文件走 multipart，其余走 JSON）。
  resources: (params: { kind?: string; limit?: number } = {}) => {
    const u = new URLSearchParams()
    if (params.kind) u.set('kind', params.kind)
    if (params.limit) u.set('limit', String(params.limit))
    const qs = u.toString()
    return request<ResourceListResponse>(`/resources${qs ? `?${qs}` : ''}`)
  },
  // INC25 / T05 —— 代码任务审批闭环：批准 → 复跑并提交（产出代码产物）；拒绝 → 销毁
  // 工作区。第三动作「重新分析」复用既有 `replanRun`（`POST /runs/{id}/replan`）。
  codeApprove: (runId: string, note = '') =>
    request<RunHandle>(`/codeplane/runs/${runId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
  codeReject: (runId: string, note = '') =>
    request<RunHandle>(`/codeplane/runs/${runId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
}

/* ------------------------------------------------------------------------- *
 * INC18-B — 结果动作「存入知识库」。
 *
 * Real backend capability: `POST /memory/store` (tenant/workspace scoped by the
 * server). Nothing here invents an endpoint; if the call fails the component
 * reports the failure verbatim instead of claiming success.
 * ------------------------------------------------------------------------- */

export type MemoryStorePayload = {
  content: string
  /** Must start with `global/` or `workspace/<id>/` (server-side ownership rule). */
  namespace: string
  metadata?: Record<string, unknown>
  ttl_hours?: number | null
}

export type MemoryStoreResult = { memory_id: string }

export async function storeMemory(payload: MemoryStorePayload): Promise<MemoryStoreResult> {
  const token = getToken()
  const res = await fetch(`${BASE}/memory/store`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
  }
  return res.json() as Promise<MemoryStoreResult>
}

/* ------------------------------------------------------------------------- *
 * INC32 / T05 — workspace surface (async dispatch · sessions · stop ·
 * artifact download). Every one of these is a REAL backend endpoint; nothing
 * is invented here.
 *
 *   POST /workspace/tasks                        → 异步派发，立即返回句柄
 *   GET  /workspace/sessions                     → 会话分组（最新活动在前）
 *   GET  /workspace/sessions/{session_id}        → 单会话的 run 头（最旧在前）
 *   POST /runs/{run_id}/abort                    → 停止（终态、不可逆）
 *   GET  /runs/{run_id}/artifacts/{artifact_id}  → 产物原文（附件下载）
 * ------------------------------------------------------------------------- */

/** The async task-create body (`POST /workspace/tasks`, INC32 ADR-01 / ADR-06). */
export type WorkspaceTaskInput = {
  intent: string
  title?: string
  workflowType?: string
  context?: Record<string, unknown>
  /** Reuse an existing conversation; empty ⇒ the backend starts a new one. */
  sessionId?: string
  /** The run this one continues (Follow-up); empty ⇒ no parent. */
  parentRunId?: string
}

/**
 * Dispatch a task **asynchronously** and return the handle at once, so the
 * caller can address the run immediately (SSE / Stop / artifacts). This is the
 * new task flow; the synchronous `hubApi.createTask` (`POST /tasks`) is left
 * unchanged on the backend and still available.
 *
 * Only the fields the caller really set are sent — an empty `session_id` /
 * `parent_run_id` is omitted so the backend keeps its own honest defaults (a
 * single-run session, no parent) rather than being handed a fabricated one.
 */
export function workspaceCreateTask(input: WorkspaceTaskInput): Promise<RunHandle> {
  const body: Record<string, unknown> = {
    intent: input.intent,
    workflow_type: input.workflowType ?? 'generic',
  }
  if (input.title) body.title = input.title
  if (input.context) body.context = input.context
  if (input.sessionId) body.session_id = input.sessionId
  if (input.parentRunId) body.parent_run_id = input.parentRunId
  return request<RunHandle>('/workspace/tasks', { method: 'POST', body: JSON.stringify(body) })
}

/** One conversation group (`GET /workspace/sessions`, INC32 ADR-02). */
export type SessionSummary = {
  session_id: string
  title: string
  created_at: string
  run_count: number
  latest_status: string
}

export type SessionList = { total: number; items: SessionSummary[] }

/** One conversation's run headers, oldest first (`GET /workspace/sessions/{id}`). */
export type WorkspaceSessionDetail = {
  session_id: string
  title: string
  runs: RunSummary[]
}

/** List the tenant's conversations (newest activity first). */
export function workspaceSessions(limit = 20): Promise<SessionList> {
  return request<SessionList>(`/workspace/sessions?limit=${limit}`)
}

/** Fetch one conversation's run headers (oldest first). */
export function workspaceSession(sessionId: string): Promise<WorkspaceSessionDetail> {
  return request<WorkspaceSessionDetail>(
    `/workspace/sessions/${encodeURIComponent(sessionId)}`,
  )
}

/** Response of `POST /runs/{run_id}/abort` (INC32 ADR-04). */
export type RunAbort = { run_id: string; status: string }

/**
 * Stop a running run. The HTTP semantics are honest and never faked (ADR-04):
 * **200** for a run that was running / awaiting-approval (or already aborted —
 * idempotent); **409** for an already terminal run; **404** for an unknown /
 * cross-tenant run; **403** for a role without `execute:workflows`. The failure
 * is surfaced verbatim through `ApiError` — never swallowed, never a success.
 */
export function abortRun(runId: string): Promise<RunAbort> {
  return request<RunAbort>(`/runs/${encodeURIComponent(runId)}/abort`, { method: 'POST' })
}

/** The real download URL for one artifact (`GET /runs/{id}/artifacts/{aid}`). */
export function artifactDownloadUrl(runId: string, artifactId: string): string {
  return `${BASE}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`
}

/**
 * Fetch one artifact's content and hand it to the browser as a file download.
 *
 * The endpoint sits behind the app's JWT/Bearer gate (RBAC fail-closed), so a
 * bare `<a download>` link would arrive **unauthenticated** — the token lives
 * in `sessionStorage`, which a plain link navigation does not attach. This
 * therefore downloads via an authenticated `fetch` → `Blob` → object-URL, which
 * really retrieves the file body (AC-31). A non-OK response is surfaced
 * verbatim through `ApiError` — never a silent failure.
 */
export async function downloadArtifact(
  runId: string,
  artifactId: string,
  filename: string,
): Promise<void> {
  const token = getToken()
  const res = await fetch(artifactDownloadUrl(runId, artifactId), {
    headers: token ? { authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename || artifactId
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/**
 * INC34 — 导出单个技能（含当前版本）为 JSON 文件（`GET /skills/{id}/export`，
 * 供团队复用 / 备份 / 迁移）。
 *
 * 与 `downloadArtifact` 同款：该端点在 JWT/Bearer 网关之后，普通 `<a download>`
 * 导航**不会**带上 sessionStorage 里的令牌，故走带鉴权的 `fetch` → `Blob` →
 * 对象 URL 下载。非 2xx 经 `ApiError` 原样上抛 —— 绝不吞错、绝不假装成功。
 */
export async function downloadSkillExport(skillId: string, filename: string): Promise<void> {
  const token = getToken()
  const res = await fetch(`${BASE}/skills/${encodeURIComponent(skillId)}/export`, {
    headers: token ? { authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body.slice(0, 200)}`)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename || `${skillId}.json`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
