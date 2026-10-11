/**
 * INC55 缺陷② —— 运行详情的轮询节奏。纯函数（无 React / 无 IO）：既供
 * `useRunDetail` 作 `refetchInterval`，也被 e2e 直接 import 断言。
 *
 * 背景（为什么必须有这个模块）：一条 run 在其生命周期内 `run_id` 不变 ⇒
 * `useRunDetail` 的 `queryKey: ['hub','run',runId]` 不变 ⇒ 默认**永不重取**。
 * 而 run 处于 `running` 时，后端 `GET /runs/{id}` 返回 `llm = {}`（空对象、
 * `detail_retained=false`）——这份「空记录」会被永久缓存：即使 run 随后
 * `completed`（后端此时才给出真实的 `llm`），首页也会一直停在「平台配置」回退态，
 * **不会**升级显示该 run 真实使用的模型。这等于「界面在说谎」。修复方式：非终态
 * 定期重取真实详情、终态即停。
 *
 * 规则（与后端 `forgeflow/workspace/store.py` 的 `TERMINAL_STATUSES` 同口径）：
 *   · `status` 为 `null` / `undefined`（详情未取到 / 无 `runId`）⇒ `false`（不空转）。
 *   · `status` ∈ `TERMINAL_RUN_STATUSES`（终态）⇒ `false`（终态即停，不浪费请求）。
 *   · 其它（`running` / `queued` / `pending` / 未知）⇒ `RUN_DETAIL_POLL_MS`。
 *
 * 诚实纪律：本模块只决定「多久重取一次真实详情」，**不**改动任何展示语义——
 * 究竟显示真实模型 / 平台配置 / 空态，仍完全由后端返回的 `llm` 决定。
 */

/**
 * 已终态的 run 状态集合，与后端 `forgeflow/workspace/store.py:47
 * TERMINAL_STATUSES` 逐字同口径（改一处必须改另一处）。
 */
export const TERMINAL_RUN_STATUSES = [
  'completed',
  'failed',
  'aborted',
  'interrupted',
  'rejected',
] as const

/** 非终态 run 的详情轮询周期（毫秒）。 */
export const RUN_DETAIL_POLL_MS = 3_000

const TERMINAL_SET: ReadonlySet<string> = new Set(TERMINAL_RUN_STATUSES)

/**
 * 计算 `useRunDetail` 的 `refetchInterval`。
 *
 * @param status 运行详情顶层 `status`；未知 / 未取到一律传 `null` / `undefined`。
 * @returns 非终态返回 `RUN_DETAIL_POLL_MS`；否则返回 `false`（不轮询）。
 */
export function runDetailRefetchInterval(status: string | null | undefined): number | false {
  if (status === null || status === undefined) return false
  if (TERMINAL_SET.has(status)) return false
  return RUN_DETAIL_POLL_MS
}
