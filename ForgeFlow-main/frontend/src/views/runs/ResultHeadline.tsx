/**
 * INC39 / 段②「一句话结论」—— 结果层首屏**主角**（最大字号，位于任务块之前）。
 *
 * 数据源与诚实纪律：
 *   * `conclusions` = `realRun.deriveConclusions(part.deliverable)` —— 交付部分「最终答案 /
 *     核心结论 / 结论」一节的**散文行**（逐字，来自产物正文）。
 *   * `conclusions.length > 0` ⇒ 逐字渲染 `conclusions[0]`（`result-headline`）。
 *   * 否则走**诚实空态**（INC39 修订）：文案 `本次运行没有生成自然语言结论`，
 *     挂 `result-headline-fallback` 标识 + `title` 说明它是状态说明、**不是产物结论**。
 *
 * ⚠️ INC39 修订（病灶修复，勿回退）：过去无真实结论时**优先渲染 `degrade.label`**
 * （如「本次运行未启用模型驱动」），于是**执行环境状态被伪装成 Agent 的一句话结论**。
 * 现在段② **不再**消费 `degrade` —— 无结论就是无结论（诚实空态）；「模型未启用」改由
 * 段① 的 `ResultEnvStatus` 作为**执行环境状态**呈现。
 *
 * ⚠️ **严禁**把状态复述伪装成产物结论：降级/空态的主标识是 `result-headline-fallback`，
 * 与真正的产物结论 `result-headline` **互斥**（同一时刻只存在一个），使两者在 DOM 上
 * 可区分（QA 可据 testid 判定「这句话到底是不是产物结论」）。
 *
 * 不变式（写进注释供 QA 判定）：
 *   `[result-headline 文本] + [...result-conclusions 各项] === conclusions 完整序列`
 *   （无重复、无丢失）—— 故本组件取 `conclusions[0]`，`result-conclusions` 渲染 `slice(1)`。
 */

/** P1-3 —— 超过该字符数时**仅视觉**截断为 2 行（全文仍由 `title` 承载，不截断数据）。 */
const HEADLINE_CLAMP_CHARS = 120

export function ResultHeadline({
  conclusions,
}: {
  /** `deriveConclusions(part.deliverable)` 的输出（逐字散文行）。 */
  conclusions: string[]
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

  // ── 诚实空态：没有结论就是没有结论（**绝不**拿降级文案 / 状态复述当结论）────────
  return (
    <p
      className="res-headline res-headline-fallback"
      data-testid="result-headline-fallback"
      title="本次运行没有可引用的产物结论；此处为状态说明，不是产物结论。"
    >
      本次运行没有生成自然语言结论
    </p>
  )
}
