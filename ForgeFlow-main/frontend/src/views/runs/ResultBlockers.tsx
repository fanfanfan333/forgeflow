/**
 * INC24 / 段④「当前阻塞」—— 只呈现**真实存在**的阻塞，绝不写空壳 / 「无阻塞」话术。
 *
 * 三个来源（互不重复）：
 *   * 4a 降级/无交付说明 —— `degrade.present` ⇒ `result-degrade-note`；否则 `!hasDeliverable`
 *     ⇒ `result-no-deliverable`。**互斥纪律不变**（同一件事只说一遍）。
 *   * 4b 缺失输入 —— `missingInputs`（`deriveMissingInputs`，`reason` 逐字）⇒ `result-missing-inputs`。
 *   * 4c 未执行步骤 —— `unrunSteps`（`deriveUnrunStepLabels`，业务名）⇒ `result-partial`。
 *   * 段底 —— `result-rerun`（「补齐后重跑」语境，`canRerun` 守卫）。
 *
 * AC-6「阻塞段不空转」：4a/4b/4c **全空** ⇒ 容器 `result-blockers` **不进入 DOM**（不渲染空壳、
 * 不写「无阻塞」）；反事实注入任一来源一条数据即变红（证明匹配有效）。
 */
import type { DegradeNotice, RunMissingInput } from './realRun'

export function ResultBlockers({
  degrade,
  hasDeliverable,
  missingInputs,
  unrunSteps,
  canRerun,
  onRerun,
  rerunPending,
  rerunError,
}: {
  degrade: DegradeNotice
  hasDeliverable: boolean
  missingInputs: RunMissingInput[]
  unrunSteps: string[]
  canRerun: boolean
  onRerun: () => void
  rerunPending: boolean
  rerunError?: string | null
}) {
  const showDegrade = degrade.present
  const showNoDeliverable = !degrade.present && !hasDeliverable
  const hasBlockers =
    showDegrade || showNoDeliverable || missingInputs.length > 0 || unrunSteps.length > 0

  // AC-6：无任何阻塞来源 ⇒ 容器不进 DOM。
  if (!hasBlockers) return null

  return (
    <section className="res-blockers" data-testid="result-blockers" aria-label="当前阻塞">
      <h4 className="res-sect-head">当前阻塞</h4>

      {/* 4a-① 降级说明：优先渲染（诚信告知**为什么**没有交付内容）。业务面只出业务表述；
           后端原始诊断串（工程原文）只作 `title` 悬浮诊断，不铺可见行。 */}
      {showDegrade && (
        <div className="res-note-block" data-testid="result-degrade-note" role="status">
          <p title={degrade.diagnostic ?? undefined}>
            {degrade.detail ? `${degrade.label}：${degrade.detail}` : degrade.label}
          </p>
        </div>
      )}

      {/* 4a-② 无交付内容（无降级说明时沿用；文案逐字不变，不是过滤产物）。 */}
      {showNoDeliverable && (
        <div className="res-note-block" data-testid="result-no-deliverable" role="status">
          <p>
            本次运行<strong>没有产出业务交付内容</strong>。平台只记录了执行过程，未生成报告正文；完整过程记录可在「执行轨迹」查看。
          </p>
        </div>
      )}

      {/* 4b 缺失输入：逐条列出受阻步骤（业务名 + `blocked_reason` 原文，逐字不加工）。 */}
      {missingInputs.length > 0 && (
        <div className="res-missing" data-testid="result-missing-inputs" role="status">
          <p className="res-missing-head">有步骤因为缺少必要声明未能执行</p>
          <ul className="res-missing-list">
            {missingInputs.map((m, i) => (
              <li key={`${m.tool}-${i}`} className="res-missing-item" title={m.tool || undefined}>
                <span className="res-missing-tool">{m.label}</span>
                {m.reason ? <span className="res-missing-reason">：{m.reason}</span> : null}
              </li>
            ))}
          </ul>
          <p className="res-missing-next">补齐上述声明后重新运行，可继续这次任务。</p>
        </div>
      )}

      {/* 4c 部分完成：计划中（除受阻外）未执行的步骤，业务名点名。 */}
      {unrunSteps.length > 0 && (
        <div className="res-partial" data-testid="result-partial" role="status">
          <p className="res-partial-head">任务部分完成</p>
          <p className="res-partial-body">
            除受阻步骤外，计划中还有 {unrunSteps.length} 个步骤未执行：{unrunSteps.join('、')}
            。已产出的内容仍可使用，但它不是一次完整交付。
          </p>
        </div>
      )}

      {/* 段底：补齐后重跑（真调后端 POST /runs/{id}/replan）。仅 `canRerun` 时渲染。 */}
      {/* INC38 —— 按钮文案去括号注释（原「重新运行（保留原有声明）」）：按钮只留动作，
          「沿用原有声明」这一必要信息挪到右侧次级说明，平实中文、不与按钮抢权重。 */}
      {canRerun && (
        <div className="res-rerun">
          <button
            type="button"
            className="btn sm"
            data-testid="result-rerun"
            onClick={onRerun}
            disabled={rerunPending}
          >
            {rerunPending ? '重新运行中…' : '重新运行'}
          </button>
          <p className="res-rerun-hint">沿用本次已填写的输入声明</p>
          {rerunError && (
            <p className="res-error" role="alert">
              {rerunError}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
