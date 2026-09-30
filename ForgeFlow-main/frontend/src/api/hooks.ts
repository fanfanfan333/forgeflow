import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  abortRun,
  api,
  fetchResourceLimits,
  hubApi,
  previewResource,
  registerResource,
  storeMemory,
  workspaceCreateTask,
  workspaceSessions,
} from './client'
import type {
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

export function useHomeKpis(): HomeKpis {
  const metrics = useMetricsSummary()
  // <<< 换源点 (KPI #3 「节省成本」): replace THIS call to re-point the savings
  //     KPI — e.g. useCostSavings() → a future /cost/savings source. The
  //     normalised shape + all downstream maths stay identical, so HomeView is
  //     untouched. This is the ONLY line to edit for a KPI#3 source swap.
  const savings = useCostSavings()
  const m = metrics.data
  const s = savings.data
  const hasData = !!m?.has_data
  const hasSavings = !!s?.has_data && s?.amount != null
  return {
    loading: metrics.isLoading || savings.isLoading,
    totalRuns: { hasData: !!m, value: m?.total_runs ?? 0 },
    successRate: {
      hasData,
      value: m?.success_rate ?? 0,
      sampleSize: m?.terminal_runs ?? 0,
    },
    avgLatencyMs: {
      hasData,
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
