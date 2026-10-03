/**
 * SkillExperienceView —— 技能资产中心「经验」页（INC46 T06）。
 *
 * 只读消费 `GET /skills/{id}/experience`：该技能当前版本的**来源经验**
 * （`SkillVersion.source_experience_ids` 逐条真读）。每条经验展示真实字段
 * （outcome / 标签 / 复用步数），**不编造**任何未携带的数据。
 *
 * 诚实纪律（红线 4 / §8）：
 *   · 未选中技能 ⇒ 诚实空态（「选择一个技能」）；
 *   · 无来源经验 ⇒ 真实空态（「该版本未记录来源经验」），**不伪造**一条经验；
 *   · 加载中 / 失败都有诚实态。
 */

import { humanizeError } from '../../api/errors'
import { useSkillExperience } from '../../api/hooks'

export function SkillExperienceView({ skillId }: { skillId: string | null }) {
  const expQ = useSkillExperience(skillId)

  if (!skillId) {
    return (
      <section className="skill-insights-block" data-testid="skill-experience">
        <div className="skill-insights-block-title">来源经验</div>
        <p className="skill-insights-empty" data-testid="skill-experience-empty">
          选择一个技能，查看它沉淀自哪些经验
        </p>
      </section>
    )
  }

  if (expQ.isLoading) {
    return (
      <section className="skill-insights-block" data-testid="skill-experience">
        <div className="skill-insights-block-title">来源经验</div>
        <p className="skill-insights-loading">来源经验加载中…</p>
      </section>
    )
  }

  if (expQ.isError) {
    return (
      <section className="skill-insights-block" data-testid="skill-experience">
        <div className="skill-insights-block-title">来源经验</div>
        <p className="skill-insights-error" role="alert">
          {humanizeError(expQ.error, '来源经验加载失败').label}
        </p>
      </section>
    )
  }

  const data = expQ.data
  const items = data?.items ?? []
  return (
    <section className="skill-insights-block" data-testid="skill-experience">
      <div className="skill-insights-block-title">
        来源经验 <span className="text-mono">（{data ? data.total : '—'}）</span>
      </div>
      {items.length > 0 ? (
        <ul className="skill-exp-list">
          {items.map((exp) => (
            <li key={exp.id} className="skill-exp-item" data-testid="skill-experience-item">
              <div className="skill-exp-head">
                <span className="skill-exp-summary">{exp.summary || '（无总结）'}</span>
                <span className={`badge ${exp.outcome === 'success' ? 'emerald' : 'amber'}`}>
                  {exp.outcome}
                </span>
              </div>
              <div className="skill-exp-meta text-mono">
                <span>复用步 {exp.reusable_steps.length}</span>
                <span>标签 {exp.tags.length}</span>
                <span>run {exp.run_id || '—'}</span>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="skill-insights-empty">该版本未记录来源经验</p>
      )}
    </section>
  )
}
