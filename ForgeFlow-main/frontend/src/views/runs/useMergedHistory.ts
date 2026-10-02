/**
 * useMergedHistory — 「首页 · 近期任务」与「/tasks · 左列历史」**共用**的两源合并源
 * （E3 / P7：消除 B3 的漂移风险）。
 *
 * 数据源（与 `LiveRunsView` 原逻辑**逐字一致**）：
 *   · 持久底  `useWorkspaceSessions(limit)` → `GET /workspace/sessions`
 *     （PG，**跨重启真实存在**，是列表的底）；
 *   · 易失补充 `useHubRuns(limit)`          → `GET /runs`
 *     （进程内 store，是运行中 / 新建 / follow-up run 的实时来源）。
 * 经 `views/runs/history.ts::mergeHistoryRuns`（纯函数）按 `run_id` 去重合并，**持久项为准**。
 *
 * ⚠️ 防漂移纪律：两处调用方**只允许**改 `limit`，**不得**各自再写一份合并实现——
 *    「首页只读易失源」正是 B3 的根因，唯一实现是修复的承重前提。
 *
 * 加载态：以**持久底**为准（`sessions.isLoading`），补充源随后并入，不产生第二次骨架闪烁。
 */
import { useMemo } from 'react'
import type { RunSummary } from '../../api/client'
import { useHubRuns, useWorkspaceSessions } from '../../api/hooks'
import { mergeHistoryRuns, sessionToRunSummary } from './history'

export function useMergedHistory(limit: number): {
  runs: RunSummary[]
  /** 以持久底为准（补充源随后并入）。 */
  loading: boolean
} {
  const hubRuns = useHubRuns(limit)
  const sessions = useWorkspaceSessions(limit)
  const runs = useMemo(
    () =>
      mergeHistoryRuns(
        (sessions.data?.items ?? []).map(sessionToRunSummary),
        hubRuns.data?.items ?? [],
      ),
    [sessions.data, hubRuns.data],
  )
  return { runs, loading: sessions.isLoading }
}
