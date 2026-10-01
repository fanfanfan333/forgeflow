/**
 * history.ts — 左列「历史」两源合并的**纯函数层**（无 React、无副作用）。
 *
 * 数据源（P0：历史切换到持久数据源）：
 *   · 持久底：`GET /workspace/sessions`（`useWorkspaceSessions`）—— workspace_runs
 *     持久表的会话分组，**跨重启真实存在**（重启后历史不再是空白）；
 *   · 易失补充：`GET /runs`（`useHubRuns`）—— 进程内 MemoryRunStore，是运行中 /
 *     新建 / follow-up run 的实时来源（run 事件 SSE 与运行中轮询逻辑不变）。
 *
 * 诚实纪律（与 realRun.ts / conversation.ts 同款）：会话摘要（SessionSummary）
 * 没有的字段**绝不伪造** —— `intent` / `outcome` / `completed_at` / `experience_id`
 * 给诚实空值；`step_count` 直接**缺席**（RunSummary.step_count 为可选，渲染方按
 * 「缺席 = 不展示」处理，绝不填 0）。
 */
import type { RunSummary, SessionSummary } from '../../api/client'

/**
 * 把一条**持久会话摘要**映射为左列的一行。
 *
 * 关键事实（INC32 ADR-02，`forgeflow/workspace/models.py`）：会话的 `session_id`
 * 就是该会话**首个 run 的 `run_id`**，因此映射行的 `run_id = session_id` 是一个
 * **真实可寻址**的运行句柄 —— 点击后沿用既有 `selectedRunId` → `GET /runs/{id}`
 * 详情通路（跨重启回填的 run 其 `detail_retained === false`，界面已有如实说明），
 * 不需要任何伪造字段。
 */
export function sessionToRunSummary(session: SessionSummary): RunSummary {
  return {
    run_id: session.session_id,
    thread_id: '', // 摘要不携带；dispatcher 重启回填同款诚实空值（绝不臆造）
    status: session.latest_status,
    outcome: '', // 摘要不携带 ⇒ 空（不伪造终局）
    intent: '', // 摘要只携带 title ⇒ 不伪造 intent
    title: session.title,
    created_at: session.created_at,
    completed_at: null, // 摘要不携带 ⇒ null
    experience_id: null, // 摘要不携带 ⇒ null
    // step_count 缺席：摘要不携带步数，绝不填 0 伪造（RunListPanel 按缺席不展示）。
    session_id: session.session_id,
  }
}

/**
 * 两源合并：**持久项为底、易失项补充**，按 `run_id` 去重且**持久项为准**。
 *
 *   · 持久项保持后端顺序（会话按最近活动倒序），构成列表主体；
 *   · 易失项（`GET /runs`，新→旧）中 `run_id` 未被持久快照覆盖的排在**最前** ——
 *     这正是「新建 / 运行中 / follow-up」的 run：它们可能尚未进入 15s 轮询的
 *     会话快照，但必须实时出现在列表里；
 *   · 同一 run 两源都有时只保留持久行（去重以持久项为准）。
 *
 * 纯函数、可单测：两个入组都可以是空数组（任一端失败 / 无数据 ⇒ 诚实退化，
 * 绝不产出不存在的行）。
 */
export function mergeHistoryRuns(persisted: RunSummary[], live: RunSummary[]): RunSummary[] {
  const persistedIds = new Set(persisted.map((r) => r.run_id))
  const supplement = live.filter((r) => !persistedIds.has(r.run_id))
  return [...supplement, ...persisted]
}
