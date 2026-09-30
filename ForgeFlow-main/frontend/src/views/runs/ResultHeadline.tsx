/**
 * INC24 / 段②「一句话结论」—— 结果层首屏**主角**（最大字号，位于 `result-body` 之前）。
 *
 * 数据源与诚实纪律（P0-2 / P1-2 / Q4）：
 *   * `conclusions` = `realRun.deriveConclusions(part.deliverable)` —— 交付部分「最终答案 /
 *     核心结论 / 结论」一节的**散文行**（逐字，来自产物正文，属 P0-2 管辖）。
 *   * `conclusions.length > 0` ⇒ 逐字渲染 `conclusions[0]`（`result-headline`）。
 *   * 否则走**诚实降级**（P1-2，零后端风险）：优先 `degrade.label`；再否则渲染
 *     「本次运行结论：{deliveryLabel}」，并挂 `result-headline-fallback` 标识 + `title`
 *     说明它**是状态复述、不是产物结论**。
 *
 * ⚠️ **严禁**把状态复述伪装成产物结论：降级态的主标识是 `result-headline-fallback`，
 * 与真正的产物结论 `result-headline` **互斥**（同一时刻只存在一个），使两者在 DOM 上
 * 可区分（QA 可据 testid 判定「这句话到底是不是产物结论」）。
 *
 * ⚠️ 确定性档 / `no_model` 降级档的后端 `_render_full_report` **不产出**「最终答案」节
 * （Q4 已核实）⇒ 这些档位 `conclusions === []` ⇒ 必然走降级路径。这是**如实降级**，
 * 不是补数据。
 *
 * 不变式（§7.6-1，写进注释供 QA 判定）：
 *   `[result-headline 文本] + [...result-conclusions 各项] === conclusions 完整序列`
 *   （无重复、无丢失）—— 故本组件取 `conclusions[0]`，`result-conclusions` 渲染 `slice(1)`。
 */

/** P1-3 —— 超过该字符数时**仅视觉**截断为 2 行（全文仍由 `title` 承载，不截断数据）。 */
const HEADLINE_CLAMP_CHARS = 120

export function ResultHeadline({
  conclusions,
  degrade,
  deliveryLabel,
}: {
  /** `deriveConclusions(part.deliverable)` 的输出（逐字散文行）。 */
  conclusions: string[]
  /** `deriveDegradeNotice(...)` 的输出；仅 `present` 时用作降级主文案。 */
  degrade: { present: boolean; label: string }
  /** 六态交付状态的中文标签（`delivery.label`），降级末档的状态复述用。 */
  deliveryLabel: string
}) {
  const first = conclusions.length > 0 ? conclusions[0] : ''

  // ── 主路径：真实产物结论（逐字）──────────────────────────────────────────
  if (first.trim()) {
    const long = first.length > HEADLINE_CLAMP_CHARS
    return (
      <p
        className={`res-headline${long ? ' res-headline-clamp' : ''}`}
        data-testid="result-headline"
        title={first}
      >
        {first}
      </p>
    )
  }

  // ── 降级路径（P1-2）：优先 degraded.label，再否则状态复述 ─────────────────
  const fromDegrade = degrade.present && degrade.label.trim().length > 0
  const text = fromDegrade ? degrade.label : `本次运行结论：${deliveryLabel}`
  return (
    <p
      className="res-headline res-headline-fallback"
      data-testid="result-headline-fallback"
      title="本次运行没有可引用的产物结论；此处为运行状态复述，不是产物结论。"
    >
      {text}
    </p>
  )
}
