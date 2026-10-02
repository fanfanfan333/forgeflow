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
 *
 * INC42 / T7 —— 新增入参 `runtimeMode` 与两条**非 env** 单行状态（缺陷④）：
 *   * `runtime_mode` 属模型驱动档（`llm` / `react` / `graph`）⇒ 渲染
 *     `result-env-connected`「执行环境：已连接模型服务」（不再被误报成未连接模型）；
 *   * `runtime_mode === ''`（历史记录 / 未记录）⇒ 渲染 `result-env-recorded`
 *     「执行环境：历史记录（运行模式未记录）」，**绝不**说「未连接模型服务」。
 *   原 `result-env-status`（未连接模型服务）块**逐字保留**，只在**明确**的
 *   `kind==='env'`（离线编排档 / `no_model` 降级）时出现 —— 既有 e2e `④a/④b/④c`
 *   口径不变（`result-env-status` 仍**不**在模型驱动档出现）。
 */
import type { DegradeNotice } from './realRun'

/** INC42 —— 模型驱动档位集合（与 `roles.ts::MODEL_RUNTIME_MODES` 同口径）。 */
const MODEL_RUNTIME_MODES: ReadonlySet<string> = new Set(['llm', 'react', 'graph'])

export function ResultEnvStatus({
  degrade,
  runtimeMode,
  canRerun,
  onRerun,
  rerunPending,
}: {
  degrade: DegradeNotice
  /** INC42 —— 本次运行的 `runtime_mode`（`''` = 历史记录 / 未记录）。 */
  runtimeMode?: string | null
  /** 「重新运行」入口可用性（受阻步骤 > 0 或无交付）。 */
  canRerun: boolean
  /** 「重新运行」回调（真调 `POST /runs/{id}/replan`）。 */
  onRerun: () => void
  /** 「重新运行」进行中（按钮禁用 + 文案切换）。 */
  rerunPending: boolean
}) {
  const mode = (runtimeMode ?? '').trim()

  // 只在**明确的模型未启用证据**（`kind === 'env'`：离线编排档 / `no_model`）时出现。
  if (degrade.present && degrade.kind === 'env') {
    return (
      <section className="res-env" data-testid="result-env-status" aria-label="执行环境状态">
        <p className="res-env-main" title={degrade.diagnostic ?? undefined}>
          执行环境：平台编排（未连接模型服务）
        </p>
        <p className="res-env-note">
          平台只记录执行过程，本次没有生成报告正文。启用模型服务后重新运行可获得完整交付物。
        </p>
        {/*
          INC-41 F-131 披露（只增不改）：此档**只**表示平台不进行模型编排，并不代表离线 /
          断网 —— 联网检索等工具仍可能调用已配置的外部服务。可见行只出业务语。
        */}
        <p className="res-env-scope">
          说明：此档仅表示平台不进行模型编排，联网检索等工具仍可能调用已配置的外部服务。
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

  // INC42 —— 其余**已呈现的降级**（真实模型调用失败 / 降级）由段④ 独占呈现，
  // 段① 不再叠加一条环境单行，避免「已连接模型服务」与段④「调用未成功」自相矛盾。
  if (degrade.present) return null

  // INC42 —— 模型驱动档：如实说明**已连接**模型服务（修复「历史记录误报未连接」）。
  if (MODEL_RUNTIME_MODES.has(mode)) {
    return (
      <p className="res-env-line" data-testid="result-env-connected" aria-label="执行环境状态">
        执行环境：已连接模型服务
      </p>
    )
  }

  // INC42 —— 历史记录 / 未知档位：诚实说明「运行模式未记录」，**不得**说未连接模型。
  if (mode === '') {
    return (
      <p className="res-env-line" data-testid="result-env-recorded" aria-label="执行环境状态">
        执行环境：历史记录（运行模式未记录）
      </p>
    )
  }

  // 其它未登记档位：不臆造，不渲染环境条。
  return null
}
