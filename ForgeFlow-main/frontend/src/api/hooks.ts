import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, hubApi } from './client'
import type { SalesLeadInput } from './client'

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

// ---- Home KPIs — single source of truth -----------------------------------
// Every KPI on the home page is resolved here so the view stays a pure
// renderer and a future data-source swap only touches this hook. `hasData`
// flags mirror the backend's explicit "no real data" signals, letting the
// view render 「—」 instead of a misleading 0 / 0.0%.

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
    mutationFn: ({ intent, workflowType }: { intent: string; workflowType?: string }) =>
      hubApi.createTask(intent, workflowType),
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
