/**
 * Home-page personalisation by role (INC2-26).
 *
 * The backend session (`getSession().role`) carries one of a small set of
 * roles; each role gets a different KPI emphasis (order of the four home KPI
 * cards) and a different set of quick-suggestion chips under the hero input.
 * Everything here is presentation config — no data, no fabricated figures.
 *
 * Unknown / anonymous sessions fall back to `viewer`, so the home page always
 * renders *something* sensible without inventing a role.
 */

export type Role = 'admin' | 'manager' | 'sales_rep' | 'viewer'

/** The four home KPI cards, keyed so the view can reorder them per role. */
export type KpiId = 'total_runs' | 'success_rate' | 'savings' | 'avg_response'

/**
 * A quick-suggestion chip under the hero input.
 *
 * When `to` is set the chip is a navigation link to that in-app route (for
 * roles that may not execute tasks); otherwise it simply fills the task input.
 */
export type Suggestion = { label: string; to?: string }

export type RoleConfig = {
  id: Role
  /** Human-facing role label (shown next to the hero greeting). */
  label: string
  /** Display order of the four KPI cards for this role. */
  kpiOrder: KpiId[]
  /** Quick-suggestion chips rendered under the hero task input. */
  suggestions: Suggestion[]
  /**
   * Whether this role may start a run at all — `POST /tasks` requires
   * `execute:workflows`. Must stay in lock-step with the backend
   * `ROLE_PERMISSIONS` so a read-only role is never offered an action that
   * would 403 (see tests/unit/test_fact_source_alignment.py).
   */
  canExecute: boolean
}

export const ROLE_CONFIG: Record<Role, RoleConfig> = {
  admin: {
    id: 'admin',
    label: '平台管理员',
    // Ops-first: reliability and latency lead, cost sits after volume.
    kpiOrder: ['success_rate', 'avg_response', 'total_runs', 'savings'],
    suggestions: [
      { label: '查看平台运行状况' },
      { label: '审计最近操作' },
      { label: '检查成本与预算' },
      { label: '管理技能资产' },
    ],
    canExecute: true,
  },
  manager: {
    id: 'manager',
    label: '团队负责人',
    // Team-first: throughput, quality, then money, then latency.
    kpiOrder: ['total_runs', 'success_rate', 'savings', 'avg_response'],
    suggestions: [
      { label: '生成本周业务报告' },
      { label: '分析销售数据并生成报告' },
      { label: '查看团队任务进度' },
      { label: '审批待处理事项' },
    ],
    canExecute: true,
  },
  sales_rep: {
    id: 'sales_rep',
    label: '销售代表',
    // Value-first: money saved leads.
    kpiOrder: ['savings', 'total_runs', 'success_rate', 'avg_response'],
    suggestions: [
      { label: '分析销售线索' },
      { label: '生成客户跟进邮件' },
      { label: '查询产品知识库' },
      { label: '创建销售报表' },
    ],
    canExecute: true,
  },
  viewer: {
    id: 'viewer',
    label: '只读访客',
    kpiOrder: ['total_runs', 'success_rate', 'savings', 'avg_response'],
    // Read-only: the viewer lacks `execute:workflows`, so a task-submitting chip
    // would 403. Each chip therefore navigates to a real in-app route
    // (router.tsx) instead of filling the input.
    suggestions: [
      { label: '查询公司知识库', to: '/knowledge' },
      { label: '查看近期任务', to: '/tasks' },
      { label: '浏览技能中心', to: '/skills' },
      { label: '了解平台能力', to: '/architecture' },
    ],
    canExecute: false,
  },
}

const DEFAULT_ROLE: Role = 'viewer'

/** Resolve a config from a raw session role string (case-insensitive). */
export function roleConfigFor(role: string | null | undefined): RoleConfig {
  const key = (role ?? '').trim().toLowerCase()
  return (ROLE_CONFIG as Record<string, RoleConfig>)[key] ?? ROLE_CONFIG[DEFAULT_ROLE]
}
