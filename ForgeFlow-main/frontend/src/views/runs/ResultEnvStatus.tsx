/**
 * INC39 / 段①「执行环境状态」—— **模型未启用**这一族的诚实呈现（用户点名要的）。
 *
 * 病灶（修复对象）：结果层过去把「本次运行未启用模型驱动」当成段② 的**Agent 一句话结论**
 * 渲染（截图里就是这样），又把同一句话在段④「当前阻塞」里印第二遍 —— 于是**执行环境状态**
 * 被伪装成了正常 Agent 结果，还自相重复。本组件把它**还原为执行环境状态**：
 *   * 位置：结果 Tab 段① 内、`result-status-line` **之后**、`result-intent` 之前；
 *   * 只在**模型未启用**（`degrade.present && degrade.kind === 'env'`）时渲染，否则不进 DOM；
 *   * 段④ 不再为这种环境态渲染一次（同一件事只说一遍）。
 *
 * 视觉：**环境状态**的观感，不是报错红块 —— 克制的浅色信息条（`var(--bg-inset)` /
 * `var(--border-subtle)`，正文用 `--fg-primary` / `--fg-secondary`，**不用** `--fg-subtle`），
 * 明暗两档对比度均 ≥ 4.5:1。
 *
 * 词表纪律（P0-5）：可见行**只出业务语**（「平台编排」「模型服务」），**不得**含
 * `model` / `llm` / `runtime_mode` / `degrade` 等工程词。后端原始诊断串（工程原文）
 * **只**进 `title`（沿用既有 `degrade.diagnostic` 口径；env 档该值为 `null`，故通常无 title）。
 *
 * data-testid（新增）：`result-env-status` / `result-env-rerun`。
 */
import type { DegradeNotice } from './realRun'

export function ResultEnvStatus({
  degrade,
  canRerun,
  onRerun,
  rerunPending,
}: {
  degrade: DegradeNotice
  /** 「重新运行」入口可用性（受阻步骤 > 0 或无交付）。 */
  canRerun: boolean
  /** 「重新运行」回调（真调 `POST /runs/{id}/replan`）。 */
  onRerun: () => void
  /** 「重新运行」进行中（按钮禁用 + 文案切换）。 */
  rerunPending: boolean
}) {
  // 只在**模型未启用**（环境态）时出现；degraded / 无降级 ⇒ 整段不进 DOM。
  if (!degrade.present || degrade.kind !== 'env') return null

  return (
    <section className="res-env" data-testid="result-env-status" aria-label="执行环境状态">
      <p className="res-env-main" title={degrade.diagnostic ?? undefined}>
        执行环境：平台编排（未连接模型服务）
      </p>
      <p className="res-env-note">
        平台只记录执行过程，本次没有生成报告正文。启用模型服务后重新运行可获得完整交付物。
      </p>
      {canRerun && (
        <button
          type="button"
          className="btn sm"
          data-testid="result-env-rerun"
          onClick={onRerun}
          disabled={rerunPending}
        >
          {rerunPending ? '重新运行中…' : '重新运行'}
        </button>
      )}
    </section>
  )
}
