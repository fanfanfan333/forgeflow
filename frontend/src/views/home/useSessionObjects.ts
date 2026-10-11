/**
 * useSessionObjects — 工作台左栏「上下文/对象树」的数据 hook（Phase B / T-B02）。
 *
 * 数据来源（**仅这两个既有端点，零新增请求**，AUDIT §14.3.1）：
 *   · `GET /workspace/sessions/{id}` → 会话标题 + run 列表（`useWorkspaceSession`）；
 *   · `GET /runs/{id}`               → 最新 run 的对象事实（`useRunDetail`）。
 *
 * 一切派生经 `inspect.ts::deriveSessionObjectTree`（纯函数）—— 本 hook 只做
 * 「取数 + 选最新 run」，**不新增任何判定**。无会话 ⇒ `tree.sessionId=null`，
 * UI 据 `hasData` 显示诚实空态。
 */
import { useMemo } from 'react'
import { useRunDetail, useWorkspaceSession } from '../../api/hooks'
import { deriveSessionObjectTree } from './inspect'
import type { SessionObjectTree } from './inspect'

export type SessionObjectsState = {
  tree: SessionObjectTree
  loading: boolean
  /** 会话响应是否已到达（`false` ⇒ 未选中会话 / 仍在加载）。 */
  hasData: boolean
}

/**
 * 会话对象树。`sessionId` 为空 ⇒ 所有查询 `enabled:false`（不发请求），返回空树。
 *
 * 「最新 run」= 会话 run 列表的**最后一条**（`GET /workspace/sessions/{id}` 按
 * oldest-first 返回，见 `api/client.ts::WorkspaceSessionDetail` 契约）。
 */
export function useSessionObjects(sessionId: string | null): SessionObjectsState {
  const session = useWorkspaceSession(sessionId)
  const runs = session.data?.runs ?? []
  const latestRunId = runs.length > 0 ? runs[runs.length - 1].run_id : null
  const latestRun = useRunDetail(latestRunId)
  const tree = useMemo(
    () => deriveSessionObjectTree(session.data ?? null, latestRun.data ?? null),
    [session.data, latestRun.data],
  )
  return {
    tree,
    loading: session.isLoading || latestRun.isLoading,
    hasData: session.data !== undefined,
  }
}
