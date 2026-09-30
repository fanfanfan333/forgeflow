/**
 * INC24 / 段⑥「详细证据（可展开）」—— 长正文与证据收进**默认折叠**的原生 `<details>`。
 *
 * 折叠纪律（P0-3 / AC-2 / AC-10）：
 *   * `<details data-testid="result-details">` **不受控**、初始**无 `open`**（不再按长度折叠，
 *     `LONG_BODY_CHARS` 已删除）。
 *   * `result-body` **元素与 testid 恒在**（交付部分为空时渲染既有诚实句）；折进 `<details>`
 *     **不改变**它的 `textContent` —— 逐字断言用 `result-body.textContent`（不受折叠影响）。
 *   * 展开后 `innerText` 含 `result-body` 全文（信息不丢）。
 *
 * 读视图口径（D-6）：主产物跟随本地编辑态（`edit.saved` 现算的交付部分）；非主产物
 * （或主产物缺失）退回页面统一算好的 `body`（`partitionArtifactBody(artifacts[0].content).deliverable`，
 * 逐字、零改写、不重排）。初始状态二者相等（运行今日恒只有一个产物）。
 *
 * 页脚（P0-9 / §1.5 Q1 / Q5）——**每项独立条件渲染**，任一不可得或为 0 ⇒ **该项不出现**：
 *   * `证据 {N}`：N = `evidenceCount`（= `evidence.length`，数据依据条数），仅 N > 0 时出现；
 *   * `步骤 {M}`：M = `stageCount`，仅 M > 0 时出现；
 *   * `耗时 {T}`：T = run 级真实墙钟（`completed_at − created_at`，由 `runWallClockMs` 算好传入），
 *     仅当 `runDurationMs !== null` **且 `> 0`** 时出现（`null`/`0` ⇒ 该项不出现）；
 *   * 三项**皆不可得** ⇒ `result-footer` **整体不渲染**（绝不显示「证据 0 / 步骤 0 / 耗时 —」）。
 *   * 页脚在折叠区内（AC-3 豁免范围），但仍坚持**业务文案**：用「秒 / 分」，绝不出现 `ms`。
 *
 * 入口按钮（决策 #5）——`result-evidence-link` / `result-trace-link` 改为**纯导航文案**
 * （「查看证据与来源」/「查看执行轨迹」），使「计数只在页脚出现一次」，仅保留各自的存在条件。
 */
import { useMemo } from 'react'
import type { RunArtifact } from '../../api/client'
import type { RunSection, RunTab } from './types'
import { formatRunDuration, partitionArtifactBody } from './realRun'
import { ResultMarkdown } from './ResultMarkdown'
import type { ArtifactEdit } from './useArtifactEdit'

export function ResultDetails({
  runId,
  artifacts,
  body,
  sections,
  evidenceCount,
  stageCount,
  runDurationMs,
  hasEvidence,
  onTabChange,
  edit,
  primary,
}: {
  runId: string
  artifacts: RunArtifact[]
  /** 页面统一算好的交付部分（`artifacts[0]`，逐字）；主产物缺失时的读视图回落。 */
  body: string
  sections: RunSection[]
  /** 数据依据条数（`evidence.length`）——页脚「证据 N」。 */
  evidenceCount: number
  /** 工作链节点数（`realStages.length`）——页脚「步骤 M」。 */
  stageCount: number
  /** run 级真实墙钟（`completed_at − created_at`），或 `null`（未测量/不可解析）——页脚「耗时 T」。 */
  runDurationMs: number | null
  /** 是否存在真实证据/来源（`sources + evidence > 0`）——`result-evidence-link` 的存在条件。 */
  hasEvidence: boolean
  onTabChange: (tab: RunTab) => void
  /** 共享编辑态（段⑤ self/ctas 与段⑥ 编辑器共用）。 */
  edit: ArtifactEdit
  /** 主产物（`pickPrimaryArtifact(artifacts)`）；读视图跟随它的本地编辑态。 */
  primary: RunArtifact | null
}) {
  const deliverableSections = sections.filter((s) => !s.engineering)

  // 读视图：主产物（且正是 `artifacts[0]`）⇒ 跟随 `edit.saved` 现算；否则用页面算好的 `body`。
  const readBody = useMemo(() => {
    const isFirst = primary && artifacts.length > 0 && primary.id === artifacts[0].id
    return isFirst ? partitionArtifactBody(edit.saved).deliverable : body
  }, [primary, artifacts, edit.saved, body])

  // 页脚：每项独立条件渲染；全空 ⇒ 不渲染整块。
  // 「耗时 T」诚实纪律：仅在**真实测得且 > 0** 时出现（决策 #1「为 0 ⇒ 该项不出现」；
  // 与 `realRun.measuredMs` 的 > 0 惯例一致）——绝不显示「耗时 0 秒」或「耗时 —」。
  const footerParts: string[] = []
  if (evidenceCount > 0) footerParts.push(`证据 ${evidenceCount}`)
  if (stageCount > 0) footerParts.push(`步骤 ${stageCount}`)
  if (runDurationMs !== null && runDurationMs > 0) {
    footerParts.push(`耗时 ${formatRunDuration(runDurationMs)}`)
  }
  const footer = footerParts.join(' · ')

  return (
    <section className="res-details" aria-label="详细证据">
      <details className="res-details-box" data-testid="result-details">
        <summary className="res-details-sum">详细证据（可展开）</summary>

        {/* 编辑态：进入编辑时在正文旁并列一个文本框（`result-body` 仍恒在，逐字不变）。 */}
        {edit.editing && (
          <div className="res-editor">
            <label className="sr-only" htmlFor={`res-editor-${runId}`}>
              编辑结果正文
            </label>
            <textarea
              id={`res-editor-${runId}`}
              className="res-editor-input"
              value={edit.draft}
              onChange={(e) => edit.setDraft(e.target.value)}
              rows={12}
            />
          </div>
        )}

        {/* 逐字正文（恒在）。交付部分为空 ⇒ 既有诚实空态句，不编内容。 */}
        <div className="res-body" data-testid="result-body">
          {readBody.trim() ? (
            <ResultMarkdown source={readBody} />
          ) : (
            <p className="res-subtle">本次交付正文为空（平台执行账本见「执行轨迹」）。</p>
          )}
        </div>

        {/* 成果结构：按产物正文里**真实存在的标题**切出的纯目录，仅在有内容时渲染。 */}
        {deliverableSections.length > 0 && (
          <section className="res-sect res-sections" data-testid="result-sections" aria-label="成果结构">
            <h4>成果结构</h4>
            <ul className="res-sec-list">
              {deliverableSections.map((s) => (
                <li key={s.id} className={`res-sec-item lv${Math.min(s.level, 4)}`}>
                  <span className="res-sec-title">{s.title}</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* 无产物：既有诚实空态（`result-body` 仍恒在，二者不冲突）。 */}
        {artifacts.length === 0 && (
          <div className="res-empty" data-testid="result-empty">
            本次运行未产出可展示的结果
          </div>
        )}

        {/* 页脚：三项各自 > 0（且耗时可得）才显示；全空则整块不渲染。 */}
        {footer && (
          <p className="res-footer" data-testid="result-footer">
            {footer}
          </p>
        )}

        {/* 入口（纯导航）：计数只在页脚出现一次，此处只承载跳转。 */}
        {(hasEvidence || stageCount > 0) && (
          <div className="res-tab-links">
            {hasEvidence && (
              <button
                type="button"
                className="res-link-btn"
                data-testid="result-evidence-link"
                onClick={() => onTabChange('evidence')}
              >
                查看证据与来源
              </button>
            )}
            {stageCount > 0 && (
              <button
                type="button"
                className="res-link-btn"
                data-testid="result-trace-link"
                onClick={() => onTabChange('trace')}
              >
                查看执行轨迹
              </button>
            )}
          </div>
        )}
      </details>
    </section>
  )
}
