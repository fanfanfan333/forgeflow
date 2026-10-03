import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  abortRun,
  api,
  archiveMemory,
  deleteResource,
  deleteSession,
  fetchResourceLimits,
  hubApi,
  previewResource,
  registerResource,
  storeMemory,
  workspaceCreateTask,
  workspaceSessions,
} from './client'
import type {
  BudgetUpsertInput,
  MemoryStorePayload,
  RegisterResourceInput,
  SalesLeadInput,
  WorkspaceTaskInput,
} from './client'

export function useRunSalesOps() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (lead: SalesLeadInput) => api.runSalesOps(lead),
    // A finished run changes runs, metrics, cost, and (usually) approvals.
    onSuccess: () => qc.invalidateQueries(),
  })
}

export function useMetricsSummary() {
  return useQuery({
    queryKey: ['metrics', 'summary'],
    queryFn: api.metricsSummary,
    refetchInterval: 15_000,
  })
}

export function useEvaluationSummary() {
  return useQuery({
    queryKey: ['metrics', 'evaluation'],
    queryFn: api.evaluationSummary,
    refetchInterval: 60_000,
  })
}

export function useRecentRuns(limit = 20) {
  return useQuery({
    queryKey: ['metrics', 'runs', limit],
    queryFn: () => api.recentRuns(limit),
    refetchInterval: 10_000,
  })
}

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 30_000,
  })
}

export function useApprovalsPending() {
  return useQuery({
    queryKey: ['approvals', 'pending'],
    queryFn: api.approvalsPending,
    refetchInterval: 10_000,
  })
}

export function useApproveMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ token, note }: { token: string; note?: string }) =>
      api.approveApproval(token, note ?? ''),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['approvals'] }),
  })
}

export function useRejectMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ token, note }: { token: string; note?: string }) =>
      api.rejectApproval(token, note ?? ''),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['approvals'] }),
  })
}

export function useAgents() {
  return useQuery({
    queryKey: ['agents'],
    queryFn: api.agents,
    refetchInterval: 30_000,
  })
}

export function useMemorySearch(q: string, namespace?: string) {
  return useQuery({
    queryKey: ['memory', 'search', q, namespace ?? null],
    queryFn: () => api.memorySearch(q, 8, namespace),
    enabled: q.trim().length > 0,
  })
}

export function useAuditSearch(action?: string) {
  return useQuery({
    queryKey: ['audit', 'search', action ?? null],
    queryFn: () => api.auditSearch({ limit: 50, action }),
    refetchInterval: 20_000,
  })
}

export function useAuditStats(days = 7) {
  return useQuery({
    queryKey: ['audit', 'stats', days],
    queryFn: () => api.auditStats(days),
    refetchInterval: 60_000,
  })
}

export function useCostByAgent(days = 7) {
  return useQuery({
    queryKey: ['cost', 'by_agent', days],
    queryFn: () => api.costByAgent(days),
    refetchInterval: 60_000,
  })
}

export function useCostByWorkflow(days = 7) {
  return useQuery({
    queryKey: ['cost', 'by_workflow', days],
    queryFn: () => api.costByWorkflow(days),
    refetchInterval: 60_000,
  })
}

export function useTopRuns(days = 7, limit = 10) {
  return useQuery({
    queryKey: ['cost', 'top_runs', days, limit],
    queryFn: () => api.topRuns(days, limit),
    refetchInterval: 60_000,
  })
}

// ---- Cost board / savings (INC2) ------------------------------------------

export function useCostBoard() {
  return useQuery({
    queryKey: ['cost', 'board'],
    queryFn: api.costBoard,
    refetchInterval: 60_000,
  })
}

export function useCostSavings() {
  return useQuery({
    queryKey: ['cost', 'savings'],
    queryFn: api.costSavings,
    refetchInterval: 60_000,
  })
}

// ---- SLO summary (INC2-07) ------------------------------------------------

export function useSloSummary() {
  return useQuery({
    queryKey: ['metrics', 'slo'],
    queryFn: api.sloSummary,
    refetchInterval: 30_000,
  })
}

// ---- Home KPIs — single source of truth (INC4 T5 / §L6 U6) ----------------
// Every KPI on the home page is resolved here so HomeView stays a pure
// renderer: no KPI maths lives in the view (no inline computation散点). Each
// field carries its own per-field `hasData` flag — has_tasks / has_success_rate
// / has_cost / has_latency — mirroring the backend's explicit "no real data"
// signals (metrics summary `has_data`, savings `has_data`), so the view renders
// 「—」 instead of a misleading 0 / 0.0% / ¥0.00.
//
// ★ SINGLE SWAP POINT (换源点) ★
// KPI #3 「节省成本」 is powered by `useCostSavings()` below (the frozen
// `/cost/savings` contract). To re-point it at a different source later, change
// the ONE line marked `<<< 换源点` inside `useHomeKpis()` — nowhere else. The
// view consumes the normalised `savings` shape (`hasData/amount/baseline/
// actual/multiplier/currency`) and needs no change.

export type HomeKpis = {
  loading: boolean
  totalRuns: { hasData: boolean; value: number }
  successRate: { hasData: boolean; value: number; sampleSize: number }
  avgLatencyMs: { hasData: boolean; value: number; sampleSize: number }
  savings: {
    hasData: boolean
    amount: number | null
    baseline: number | null
    actual: number | null
    multiplier: number
    currency: string
  }
}

/**
 * The page size the home page uses for its 「近期任务」 rail. Exported so the KPI
 * hook and the rail share **one** `['runs','recent',n]` query key (TanStack
 * dedupes identical keys) — the 总任务数 KPI must read the same source as the
 * list it sits next to, or the two disagree on screen.
 */
export const RECENT_RUNS_LIMIT = 6

export function useHomeKpis(): HomeKpis {
  const metrics = useMetricsSummary()
  // <<< 换源点 (KPI #3 「节省成本」): replace THIS call to re-point the savings
  //     KPI — e.g. useCostSavings() → a future /cost/savings source. The
  //     normalised shape + all downstream maths stay identical, so HomeView is
  //     untouched. This is the ONLY line to edit for a KPI#3 source swap.
  const savings = useCostSavings()
  // INC-AUDIT —— 「总任务数」**换源**：改与同屏「近期任务」列表同源（都是 `GET /runs`）。
  // 原先读 `/metrics/` 的 `total_runs`：postgres 档的该读数是 `run_metrics` 聚合，
  // 而 hub 路径（`POST /tasks`）**从不写 run_metrics** ⇒ 该字段结构性恒 0，同屏列表
  // 却有数据 —— 同一屏两个数字互相矛盾（验收 P1）。改读 `/runs` 的 `total` 后，两个
  // 数字出自**同一个响应**，结构上不可能再矛盾。
  const runs = useRecentHubRuns(RECENT_RUNS_LIMIT)
  const m = metrics.data
  const s = savings.data
  const hasData = !!m?.has_data
  const hasSavings = !!s?.has_data && s?.amount != null
  const totalRuns = runs.data?.total ?? 0
  return {
    loading: metrics.isLoading || savings.isLoading || runs.isLoading,
    // hasData is derived from the **total itself** (not from "a response arrived"):
    // `!!m` used to render a bare `0` even when nothing was measured.
    totalRuns: { hasData: totalRuns > 0, value: totalRuns },
    successRate: {
      // INC-41 F-122 口径同步：后端 `source=workspace_runs` 档只用终态 run 作分母，
      // 且逐字段带 `has_success_rate` —— 它才代表「成功率可测」。旧响应/桩无该字段时
      // 回退到 `hasData`（向后兼容，行为不变）。
      hasData: m?.has_success_rate ?? hasData,
      value: m?.success_rate ?? 0,
      sampleSize: m?.terminal_runs ?? 0,
    },
    avgLatencyMs: {
      // workspace_runs 没有 latency 列 ⇒ 后端如实置 has_latency=false、值 0；
      // 这里必须按 `has_latency` 判定，否则会把「未测量」渲染成 0ms（伪造测量）。
      hasData: m?.has_latency ?? hasData,
      value: m?.avg_latency_ms ?? 0,
      sampleSize: m?.total_runs ?? 0,
    },
    savings: {
      hasData: hasSavings,
      amount: s?.amount ?? null,
      baseline: s?.baseline ?? null,
      actual: s?.actual ?? null,
      multiplier: s?.multiplier ?? 1,
      currency: s?.currency ?? 'CNY',
    },
  }
}

// ---- AgentFlow hub hooks (docs/sop/02-ARCHITECTURE.md §7.3) ----------------

export function useAgentCatalog() {
  return useQuery({
    queryKey: ['agents', 'catalog'],
    queryFn: hubApi.agentCatalog,
    refetchInterval: 60_000,
  })
}

// ---- Real hub runs (智能任务) ----------------------------------------------
// `GET /runs` (list) + `GET /runs/{id}` (detail) ARE the real task timeline —
// they return the run's actual steps, errors and tool invocations. The /tasks
// page used to hard-code a demo run on the strength of a file comment claiming
// no such endpoint existed; it does.

export function useHubRuns(limit = 20) {
  return useQuery({
    queryKey: ['hub', 'runs', limit],
    queryFn: () => hubApi.recentRuns(limit),
    refetchInterval: 15_000,
  })
}

/**
 * One run's real detail. `runId` is nullable so a caller can mount the hook
 * unconditionally; `enabled` stops it firing a request for "nothing selected"
 * (which would 404 and surface as a spurious error banner).
 */
export function useRunDetail(runId: string | null) {
  return useQuery({
    queryKey: ['hub', 'run', runId],
    queryFn: () => hubApi.runDetail(runId as string),
    enabled: !!runId,
  })
}

// NOTE: creating a task is `useCreateTask` further down (it predates this
// block) — `POST /tasks` drives the run to a terminal state before responding,
// so callers must render a busy state.

export function useFeaturedSkills(limit = 4) {
  return useQuery({
    queryKey: ['skills', 'featured', limit],
    queryFn: () => hubApi.featuredSkills(limit),
    refetchInterval: 60_000,
  })
}

export function useSecurityOverview() {
  return useQuery({
    queryKey: ['security', 'overview'],
    queryFn: hubApi.securityOverview,
    refetchInterval: 30_000,
  })
}

export function useHubSkills(params: { domain?: string; q?: string; featured?: boolean; limit?: number } = {}) {
  return useQuery({
    queryKey: ['skills', 'list', params],
    queryFn: () => hubApi.skills(params),
    refetchInterval: 30_000,
  })
}

export function useSkillVersions(skillId: string | null) {
  return useQuery({
    queryKey: ['skills', 'versions', skillId],
    queryFn: () => hubApi.skillVersions(skillId as string),
    enabled: !!skillId,
  })
}

export function useSkillCandidates(params: { status?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: ['skill-candidates', params],
    queryFn: () => hubApi.candidates(params),
    refetchInterval: 30_000,
  })
}

export function useRecentHubRuns(limit = 8) {
  return useQuery({
    queryKey: ['runs', 'recent', limit],
    queryFn: () => hubApi.recentRuns(limit),
    refetchInterval: 10_000,
  })
}

export function useExperiences(
  params: { outcome?: string; tag?: string; limit?: number } = {},
) {
  return useQuery({
    queryKey: ['experiences', params],
    queryFn: () => hubApi.experiences(params),
    refetchInterval: 30_000,
  })
}

/**
 * One run's real experiences + their memory ids (INC20 / P1-3).
 *
 * `GET /experiences?run_id=` is a REAL, already-supported query parameter
 * (`experiences.py:42-57`); `hubApi.experiences` already forwards it. This hook
 * only adds the run-scoped query. `runId` is nullable so the caller can enable it
 * only when the run genuinely has an `experience_id` (avoiding a useless round-trip).
 */
export function useRunExperiences(runId: string | null) {
  return useQuery({
    queryKey: ['experiences', 'run', runId],
    queryFn: () => hubApi.experiences({ run_id: runId as string, limit: 20 }),
    enabled: !!runId,
  })
}

export function useMemoryScopes() {
  return useQuery({
    queryKey: ['memory', 'scopes'],
    queryFn: hubApi.memoryScopes,
    staleTime: 5 * 60_000,
  })
}

export function useMemoryList(params: { scope?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: ['memory', 'list', params],
    queryFn: () => hubApi.memoryList(params),
    refetchInterval: 30_000,
  })
}

export function usePolicies(params: { subject?: string; resource?: string } = {}) {
  return useQuery({
    queryKey: ['policies', params],
    queryFn: () => hubApi.policies(params),
    refetchInterval: 30_000,
  })
}

export function useHubApprovals(params: { status?: string; risk_level?: string } = {}) {
  return useQuery({
    queryKey: ['hub-approvals', params],
    queryFn: () => hubApi.approvals(params),
    refetchInterval: 15_000,
  })
}

export function useCreateTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      intent,
      workflowType,
      context,
    }: {
      intent: string
      workflowType?: string
      /** INC14 — optional run context ("继续执行" passes the previous run id). */
      context?: Record<string, unknown>
    }) => hubApi.createTask(intent, workflowType, context),
    onSuccess: () => qc.invalidateQueries(),
  })
}

export function useReplanRun() {
  const qc = useQueryClient()
  return useMutation({
    /**
     * INC22 W3.3 — re-run a task's intent via `POST /runs/{id}/replan`. The
     * backend re-declares the **original** workflow type + explicit inputs, so a
     * run that was `blocked` for a missing declaration can genuinely execute on
     * replay. Callers must never swallow a failure — surface it honestly.
     */
    mutationFn: ({ runId, reason }: { runId: string; reason?: string }) =>
      hubApi.replanRun(runId, reason),
    onSuccess: () => qc.invalidateQueries(),
  })
}

export function useCompileCandidate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { experience_ids?: string[]; mode?: string } = {}) =>
      hubApi.compileCandidate(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['skill-candidates'] }),
  })
}

export function useEvaluateCandidate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ candidateId, dataset }: { candidateId: string; dataset?: string }) =>
      hubApi.evaluateCandidate(candidateId, dataset),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['skill-candidates'] }),
  })
}

export function usePromoteCandidate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ candidateId, actor }: { candidateId: string; actor?: string }) =>
      hubApi.promoteCandidate(candidateId, actor),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['skill-candidates'] })
      qc.invalidateQueries({ queryKey: ['skills'] })
    },
  })
}

export function useRollbackSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ skillId, toVersion }: { skillId: string; toVersion: string }) =>
      hubApi.rollbackSkill(skillId, toVersion),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['skills'] }),
  })
}

/* ---- INC43 S3 / T03 —— 技能工程闭环（把后端闭环接到 UI，消除孤儿能力） ------- */

/**
 * 中栏主体（技能 / 候选）的工程事实来源标识。
 *
 * 刻意**不**引入 `views/skills/skillAssets.ts::SkillSubject` —— 避免 `api → views`
 * 的反向依赖；调用方只需投影出 `{ kind, id }`。
 */
export type EngineeringSubject =
  | { kind: 'skill'; id: string }
  | { kind: 'candidate'; id: string }

/**
 * 选中主体的工程契约 / 评审 / 用例 / 评估（只读）。
 *   技能 ⇒ `GET /skills/{id}/engineering`；
 *   候选 ⇒ `GET /skill-candidates/{id}/engineering`。
 * `subject` 为空 ⇒ `enabled:false`（不发请求）。
 */
export function useSkillEngineering(subject: EngineeringSubject | null) {
  const kind = subject?.kind ?? 'none'
  const id = subject?.id ?? ''
  return useQuery({
    queryKey: ['skill-engineering', kind, id],
    queryFn: () =>
      subject!.kind === 'skill'
        ? hubApi.skillEngineering(subject!.id)
        : hubApi.candidateEngineering(subject!.id),
    enabled: !!subject,
  })
}

/**
 * 技能主体的六态生命周期投影（`GET /skills/{id}/lifecycle`）。
 * 候选**没有**该端点 ⇒ 传 `null` 即不请求（候选的生命周期由工程响应携带）。
 */
export function useSkillLifecycle(skillId: string | null) {
  return useQuery({
    queryKey: ['skill-lifecycle', skillId],
    queryFn: () => hubApi.skillLifecycle(skillId as string),
    enabled: !!skillId,
  })
}

/**
 * 触发候选的工程闭环（`POST /skill-candidates/{id}/engineering`，**不含发布**）。
 * 成功后失效工程查询与候选列表，使中栏重取最新事实。
 */
export function useRunCandidateEngineering() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (candidateId: string) => hubApi.runCandidateEngineering(candidateId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['skill-engineering'] })
      qc.invalidateQueries({ queryKey: ['skill-candidates'] })
    },
  })
}

/* ---- INC46 T06 —— 技能洞察（rules / experience / readiness）+ 锻造 ---------- */

/**
 * 一个技能的租户规则（`GET /skills/{id}/rules`，只读）。
 * `skillId` 为空 ⇒ `enabled:false`（未选中技能时不发请求）。
 */
export function useSkillRules(skillId: string | null) {
  return useQuery({
    queryKey: ['skill-rules', skillId],
    queryFn: () => hubApi.skillRules(skillId as string),
    enabled: !!skillId,
  })
}

/** 一个技能的来源经验（`GET /skills/{id}/experience`，只读）。 */
export function useSkillExperience(skillId: string | null) {
  return useQuery({
    queryKey: ['skill-experience', skillId],
    queryFn: () => hubApi.skillExperience(skillId as string),
    enabled: !!skillId,
  })
}

/** 一个技能的就绪事实（`GET /skills/{id}/readiness`，只读；`rate` 未测量 ⇒ `null`）。 */
export function useSkillReadiness(skillId: string | null) {
  return useQuery({
    queryKey: ['skill-readiness', skillId],
    queryFn: () => hubApi.skillReadiness(skillId as string),
    enabled: !!skillId,
  })
}

/** 从真实经验锻造一个技能候选（`POST /skills/forge`）。成功后刷新候选列表。 */
export function useForgeSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { experience_ids?: string[]; mode?: string } = {}) =>
      hubApi.forgeSkill(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['skill-candidates'] }),
  })
}

/** 读回一条锻造记录（`GET /skills/forge/{id}`）；`forgeId` 为空 ⇒ 不发请求。 */
export function useForgeResult(forgeId: string | null) {
  return useQuery({
    queryKey: ['skill-forge', forgeId],
    queryFn: () => hubApi.forgeResult(forgeId as string),
    enabled: !!forgeId,
  })
}

/** 只读发布联锁状态（`GET /evolution/interlock`，T15）。无数据 ⇒ 调用方渲染「—」。 */
export function usePublishInterlock() {
  return useQuery({
    queryKey: ['evolution', 'interlock'],
    queryFn: hubApi.publishInterlock,
    refetchInterval: 30_000,
  })
}

export function useDecideApproval() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      approvalId,
      decision,
      note,
    }: {
      approvalId: string
      decision: 'approve' | 'reject'
      note?: string
    }) => hubApi.decideApproval(approvalId, decision, note ?? ''),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hub-approvals'] })
      qc.invalidateQueries({ queryKey: ['security'] })
    },
  })
}

/**
 * INC18-B — 结果动作「存入知识库」：真实调用 `POST /memory/store`。
 *
 * A failed store is surfaced through `error` — the component renders its real
 * message rather than a fake "已保存".
 */
export function useStoreMemory() {
  return useMutation({
    mutationFn: (payload: MemoryStorePayload) => storeMemory(payload),
  })
}

// ---- Resource Center (INC25 W1) -------------------------------------------

/**
 * The tenant's registered resources (`GET /resources`). Polled gently so a
 * resource registered in another tab shows up; `kind` filters server-side.
 */
export function useResources(params: { kind?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: ['resources', params],
    queryFn: () => hubApi.resources(params),
    refetchInterval: 30_000,
  })
}

/**
 * Register one resource. A failure is surfaced through `error` (the component
 * renders the backend's verbatim reason) — never swallowed, never a fake success.
 */
export function useRegisterResource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: RegisterResourceInput) => registerResource(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['resources'] }),
  })
}

/**
 * INC26 T01/T02 —— 上传上限 + 类型白名单（`GET /resources/limits`）。
 *
 * 值来自后端单一事实源，供上传前预检使用；`staleTime` 长一点（上限很少变），避免
 * 每次挂载都请求。前端**不得**写死这两个值。
 */
export function useResourceLimits() {
  return useQuery({
    queryKey: ['resources', 'limits'],
    queryFn: fetchResourceLimits,
    staleTime: 5 * 60_000,
  })
}

/**
 * INC26 T03 —— 一个资源的内容预览（`GET /resources/{id}/preview?n=`）。
 *
 * `id` 可空，`enabled` 让「未选中任何资源」时不发请求。预览是对既有端点的**真实**调用，
 * 表格/文本逐字呈现，`truncated` 驱动截断提示，非文件类给后端诚实空态。
 */
export function useResourcePreview(id: string | null, n = 20) {
  return useQuery({
    queryKey: ['resources', 'preview', id, n],
    queryFn: () => previewResource(id as string, n),
    enabled: !!id,
  })
}

/**
 * INC40 — delete one resource (`DELETE /resources/{id}`). Refreshes the resource
 * list on success. A failure (404 / 403 / …) surfaces through `error` — callers
 * must render the real reason, never a fake success.
 */
export function useDeleteResource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteResource(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['resources'] }),
  })
}

/**
 * INC40 — create or replace one budget (`POST /cost/budgets`, upsert). Refreshes
 * the cost board on success; failures surface verbatim through `error`.
 */
export function useUpsertBudget() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: BudgetUpsertInput) => hubApi.upsertBudget(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['cost', 'board'] }),
  })
}

/**
 * INC40 — delete one budget (`DELETE /cost/budgets/{scope}`). Refreshes the cost
 * board on success; a 404 (no such budget) surfaces through `error`.
 */
export function useDeleteBudget() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { scope: string; scopeId?: string | null }) => hubApi.deleteBudget(v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['cost', 'board'] }),
  })
}

/**
 * INC40 — archive one hub memory (`POST /memory/{id}/archive`). Marker-only
 * (the platform never physically deletes a memory). Refreshes the memory list.
 */
export function useArchiveMemory() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => archiveMemory(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['memory'] }),
  })
}

/**
 * INC25 / T05 —— 代码任务的审批决定（`approve` / `reject`）。
 *
 * 两个动作都**真调后端**（`POST /codeplane/runs/{id}/approve|reject`）：批准触发复跑
 * （产出代码产物并选中新 run），拒绝销毁工作区。第三动作「重新分析」不在这里 ——
 * 它复用既有 `useReplanRun`（`POST /runs/{id}/replan`）。
 */
export function useCodeDecision() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, action, note }: { runId: string; action: 'approve' | 'reject'; note?: string }) =>
      action === 'approve' ? hubApi.codeApprove(runId, note ?? '') : hubApi.codeReject(runId, note ?? ''),
    onSuccess: () => qc.invalidateQueries(),
  })
}

// ---- INC32 / T05 — workspace: async create · sessions · stop --------------

/**
 * 异步创建任务（`POST /workspace/tasks`）。与同步 `useCreateTask`（`POST /tasks`）
 * 不同，本通路**立即**返回运行句柄（`run_id`），调用方据此边跑边看（SSE / 停止 / 产物）。
 *
 * 诚实守卫（P0-2 / 数据诚实）：后端若不返回 `run_id`（例如被错误 stub 的响应），
 * **绝不伪造成功** —— 抛错让调用方进入诚实错误态，而不是拿一个空句柄去「选中」一个
 * 并不存在的运行。
 */
export function useWorkspaceCreateTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: WorkspaceTaskInput) => {
      const handle = await workspaceCreateTask(input)
      if (!handle || !handle.run_id) {
        throw new Error('任务创建失败：后端未返回运行句柄（run_id 缺失）')
      }
      return handle
    },
    onSuccess: () => qc.invalidateQueries(),
  })
}

/** 租户的会话分组（`GET /workspace/sessions`）。 */
export function useWorkspaceSessions(limit = 20) {
  return useQuery({
    queryKey: ['workspace', 'sessions', limit],
    queryFn: () => workspaceSessions(limit),
    refetchInterval: 15_000,
  })
}

/**
 * INC42 —— 软删除一个会话（`DELETE /workspace/sessions/{session_id}`）。
 *
 * 后端为**软删除**（写 `deleted_at` 标记，行保留以存审计链），成功后刷新持久会话底
 * （`['workspace','sessions']`）与易失补充源（`['runs']` / `['hub']`），使首页「近期任务」
 * 立即不再显示该会话。失败（404 / 403 / …）经 `error` 原样上抛 —— 调用方**不吞错**、
 * **不假装成功**。
 */
export function useDeleteSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sessionId: string) => deleteSession(sessionId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workspace', 'sessions'] })
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['hub'] })
    },
  })
}

/**
 * 停止一个运行中的任务（`POST /runs/{run_id}/abort`）。终态、不可逆；失败经 `error`
 * 如实上抛（403 / 404 / 409 各有真实码），调用方**不吞错**、**不假装成功**。
 */
export function useAbortRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => abortRun(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hub'] })
      qc.invalidateQueries({ queryKey: ['runs'] })
    },
  })
}

// ---- INC34 —— 技能资产沉淀：手工创建技能 / 新建版本 --------------------------

/**
 * 手工创建一个技能（`POST /skills`）。成功后刷新技能列表。
 *
 * 失败经 `error` 原样上抛（如 409 名称已存在），调用方**不吞错**、**不假装成功**。
 */
export function useCreateSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; domain: string; owner?: string; description?: string }) =>
      hubApi.createSkill(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['skills'] }),
  })
}

/**
 * 为一个技能创建新版本（`POST /skills/{id}/versions`）。
 *
 * `spec.io_schema` 非法时后端返回 **400** 且 detail 为中文可读原因 —— 经 `error`
 * 透出，界面逐字展示（不收敛成「参数错误」）。
 */
export function useCreateSkillVersion() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      skillId,
      spec,
      changelog,
      bump,
    }: {
      skillId: string
      spec: Record<string, unknown>
      changelog?: string
      bump?: string
    }) => hubApi.createSkillVersion(skillId, { spec, changelog, bump }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['skills'] })
      qc.invalidateQueries({ queryKey: ['skills', 'versions'] })
    },
  })
}

// ---- INC34 —— 技能市场（真实消费后端接口）-----------------------------------

/** 市场里可浏览/安装的技能（`GET /marketplace/skills`）。 */
export function useMarketplaceListings(
  params: { q?: string; domain?: string; cross_tenant?: boolean; limit?: number } = {},
) {
  return useQuery({
    queryKey: ['marketplace', 'listings', params],
    queryFn: () => hubApi.marketplaceListings(params),
    refetchInterval: 30_000,
  })
}

/**
 * 把技能上架到市场（`POST /marketplace/skills/publish`）。
 *
 * 上架前有 DLP + 可信基线双预检；被拒是 **403** 且带具体原因（或跨租户共享缺
 * `approve:skills`）—— 经 `error` 原样上抛，界面逐字展示。
 */
export function usePublishListing() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { skill_id: string; shared?: boolean; description?: string; version?: string }) =>
      hubApi.publishListing(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['marketplace'] }),
  })
}

/** 把一条市场技能安装到当前租户（`POST /marketplace/skills/{id}/install`）。 */
export function useInstallListing() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (listingId: string) => hubApi.installListing(listingId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['marketplace'] })
      qc.invalidateQueries({ queryKey: ['skills'] })
    },
  })
}

/** 给一条市场技能评分（`POST /marketplace/skills/{id}/rate`，1–5）。 */
export function useRateListing() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ listingId, score, comment }: { listingId: string; score: number; comment?: string }) =>
      hubApi.rateListing(listingId, { score, comment }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['marketplace'] }),
  })
}

/** 工作流模板（`GET /marketplace/templates`）。 */
export function useMarketplaceTemplates(params: { domain?: string; tag?: string } = {}) {
  return useQuery({
    queryKey: ['marketplace', 'templates', params],
    queryFn: () => hubApi.marketplaceTemplates(params),
    staleTime: 60_000,
  })
}

/** 重新扫描模板目录（`POST /marketplace/templates/refresh`）。 */
export function useRefreshTemplates() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => hubApi.refreshTemplates(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['marketplace', 'templates'] }),
  })
}
