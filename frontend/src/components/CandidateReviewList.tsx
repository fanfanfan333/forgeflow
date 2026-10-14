import type { SkillCandidate } from '../api/client'
import { DeleteSkill } from './DeleteSkill'
import { IconSparkle } from './icons'

const DOMAINS: Record<string, string> = { generic: '通用工作方法', document: '文档处理', data: '数据分析', code: '代码处理' }
const STATES: Record<string, string> = { draft: '待补充指令', evaluating: '评估中', insufficient: '经验不足', published: '已发布', failed: '评估未通过' }

export function CandidateReviewList({ items, search, loading, onOrganize }: { items: SkillCandidate[]; search: string; loading: boolean; onOrganize: (candidate: SkillCandidate) => void }) {
  const matches = items.filter(c => (c.name + ' ' + c.domain + ' ' + String(c.draft_spec?.prompt || '')).toLowerCase().includes(search.trim().toLowerCase()))
  if (loading) return <p className="skill-library-note">正在读取任务经验…</p>
  return <>
    <div className="candidate-review-list">{matches.map(candidate => {
      const title = candidate.name.startsWith('generic') ? (DOMAINS[candidate.domain] || candidate.domain || '任务经验') : candidate.name
      const prompt = String(candidate.draft_spec?.prompt || '')
      const provenance = candidate.draft_spec?.learning_provenance
      const learned = typeof provenance === 'object' && provenance !== null && 'acceptance_verified' in provenance && provenance.acceptance_verified === true
      const state = learned ? '待功能验证' : (STATES[candidate.status] || candidate.status)
      const description = typeof candidate.draft_spec?.description === 'string' ? candidate.draft_spec.description : prompt.split('\n').find(line => line.trim() && !line.startsWith('#'))
      return <article className="candidate-review-row" key={candidate.id}>
        <div className="candidate-review-icon"><IconSparkle width={19} height={19} /></div>
        <div className="candidate-review-main"><h2>{title}<small>#{candidate.id.slice(0, 7)}</small></h2>
          <p>{description?.slice(0, 140) || '尚未形成工作指令。查看来源经验，补充处理方法后再使用。'}</p>
          {learned && <p className="skill-library-note">从已验收任务自动整理 · 待实际功能验证 · 尚未启用</p>}
          <div className="candidate-review-meta"><span>{state}</span>{Array.isArray(candidate.experience_ids) && <span>{candidate.experience_ids.length} 条来源经验</span>}{candidate.created_at && <time>{new Date(candidate.created_at).toLocaleDateString('zh-CN')}</time>}{candidate.status === 'insufficient' && candidate.required_experiences != null && <span>至少需要 {candidate.required_experiences} 条相似经验</span>}</div>
          <details><summary>查看指令与来源</summary><p className="candidate-instructions">{prompt || '暂无指令，整理时需要补充。'}</p>{candidate.experience_ids?.length > 0 && <p className="candidate-source-ids">来源编号：{candidate.experience_ids.join('、')}</p>}</details>
        </div>
        <div className="candidate-review-actions"><button className="candidate-organize" onClick={() => onOrganize(candidate)}>整理为技能</button><DeleteSkill id={candidate.id} name={title} candidate onDeleted={() => {}} /></div>
      </article>
    })}</div>
    {!!items.length && !matches.length && <p className="skill-library-empty">没有找到匹配的经验，试试其他关键词。</p>}
  </>
}
