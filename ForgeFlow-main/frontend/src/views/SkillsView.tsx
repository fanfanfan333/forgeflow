/**
 * SkillsView — 技能中心 (Skill Hub, PRD §6.1 / T17).
 *
 * Shows the skill registry (search + domain filter + versions + rollback) and
 * the skill-candidate pipeline: 经验 → 编译候选 → 评估 → 固化(promote).
 * Every panel is backed by the real hub API.
 */

import { useState } from 'react'
import {
  useCompileCandidate,
  useEvaluateCandidate,
  useExperiences,
  useHubSkills,
  usePromoteCandidate,
  useRollbackSkill,
  useSkillCandidates,
  useSkillVersions,
} from '../api/hooks'
import type { Skill, SkillCandidate } from '../api/client'
import { IconSparkle } from '../components/icons'
import '../styles/skills.css'

const DOMAINS = ['', '数据分析', '企业知识库', '项目管理', '代码开发', 'general']

export function SkillsView() {
  const [q, setQ] = useState('')
  const [domain, setDomain] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const skillsQ = useHubSkills({ q: q || undefined, domain: domain || undefined, limit: 60 })
  const skills = skillsQ.data?.items ?? []

  return (
    <section className="view active" data-screen-label="技能中心">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>技能中心</h1>
            <p className="sub">
              沉淀团队经验，构建可复用的技能资产 · 数据来自技能接口
            </p>
          </div>
          <div className="actions">
            <a className="btn sm" href="/knowledge">查看经验来源 →</a>
          </div>
        </div>
      </div>
      <div className="page-body hub">
        <div className="hub-toolbar">
          <label className="hub-search">
            <span aria-hidden="true">⌕</span>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索技能名称或描述…"
              aria-label="搜索技能"
            />
          </label>
          <select
            className="hub-select"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            aria-label="领域筛选"
          >
            {DOMAINS.map((d) => (
              <option key={d} value={d}>
                {d === '' ? '全部领域' : d}
              </option>
            ))}
          </select>
          <span className="text-mono text-muted text-12">
            {skillsQ.isLoading ? '加载中…' : `${skills.length} 个技能`}
          </span>
        </div>

        {skillsQ.isError ? (
          <div className="card empty">加载失败：{(skillsQ.error as Error)?.message}</div>
        ) : skills.length === 0 && !skillsQ.isLoading ? (
          /* INC34 轮2 — SVG 星芒替换 `✦` 文本符号。 */
          <div className="card empty"><span className="big"><IconSparkle width={22} height={22} /></span>暂无技能，先从经验编译一个候选吧</div>
        ) : (
          <div className="skill-grid">
            {skills.map((s) => (
              <SkillTile
                key={s.id}
                skill={s}
                selected={selected === s.id}
                onSelect={() => setSelected(selected === s.id ? null : s.id)}
              />
            ))}
          </div>
        )}

        {selected && <VersionPanel skillId={selected} />}

        <CandidateSection />
      </div>
    </section>
  )
}

function SkillTile({
  skill,
  selected,
  onSelect,
}: {
  skill: Skill
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      className={`skill-tile${selected ? ' selected' : ''}`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <div className="st-head">
        <span className="st-name">{skill.name}</span>
        <span className="badge purple">{skill.domain}</span>
      </div>
      <span className="st-desc">{skill.description || '暂无描述'}</span>
      <div className="st-foot">
        <span>v{skill.current_version ?? '—'}</span>
        <span>·</span>
        <span>{skill.usage_count} 次使用</span>
        <span style={{ marginLeft: 'auto' }}>
          <span className={`badge ${skill.status === 'published' ? 'emerald' : ''}`}>{skill.status}</span>
        </span>
      </div>
    </button>
  )
}

function VersionPanel({ skillId }: { skillId: string }) {
  const versionsQ = useSkillVersions(skillId)
  const rollback = useRollbackSkill()
  const versions = versionsQ.data ?? []

  return (
    <div className="panel" style={{ marginTop: 20 }}>
      <div className="panel-head">
        <div className="title">版本历史</div>
        <div className="actions">
          <span>{versionsQ.isLoading ? '加载中…' : `${versions.length} 个版本`}</span>
        </div>
      </div>
      <div className="panel-body">
        {versions.length === 0 && !versionsQ.isLoading ? (
          <div className="empty">该技能暂无历史版本</div>
        ) : (
          <div className="ver-list">
            {versions.map((v) => (
              <div key={v.id} className="ver-item">
                <span className="semver">v{v.semver}</span>
                <span className="changelog">{v.changelog || '—'}</span>
                <span className="meta">
                  {v.eval_score != null ? `评分 ${v.eval_score.toFixed(2)}` : '未评估'} · {v.approved_by ?? '—'}
                </span>
                <button
                  type="button"
                  className="btn sm"
                  disabled={rollback.isPending}
                  onClick={() =>
                    rollback.mutate({ skillId, toVersion: v.semver })
                  }
                >
                  回滚至此
                </button>
              </div>
            ))}
          </div>
        )}
        {rollback.isError && (
          <p className="text-12" style={{ color: 'var(--danger-fg)', marginTop: 8 }}>
            回滚失败：{(rollback.error as Error)?.message}
          </p>
        )}
      </div>
    </div>
  )
}

function CandidateSection() {
  const candidatesQ = useSkillCandidates({ limit: 30 })
  const experiencesQ = useExperiences({ limit: 1 })
  const compile = useCompileCandidate()
  const candidates = candidatesQ.data?.items ?? []
  const experienceTotal = experiencesQ.data?.total ?? 0

  return (
    <div className="panel" style={{ marginTop: 20 }}>
      <div className="panel-head">
        <div className="title">技能候选 · 跳③ 编译器</div>
        <div className="actions">
          <span>{experienceTotal} 条经验可用</span>
          <button
            type="button"
            className="btn sm primary"
            disabled={compile.isPending}
            onClick={() => compile.mutate({ mode: 'auto' })}
          >
            {compile.isPending ? '编译中…' : '+ 从经验生成候选'}
          </button>
        </div>
      </div>
      <div className="panel-body">
        {compile.isError && (
          <p className="text-12" style={{ color: 'var(--amber-4)', marginBottom: 12 }}>
            编译未完成：{(compile.error as Error)?.message}
          </p>
        )}
        {compile.isSuccess && compile.data?.status === 'insufficient' && (
          <p className="text-12" style={{ color: 'var(--amber-4)', marginBottom: 12 }}>
            相似经验不足（{compile.data.experience_ids.length} 条），暂无法固化为技能。
          </p>
        )}
        {candidates.length === 0 && !candidatesQ.isLoading ? (
          <div className="empty">
            <span className="big">✧</span>暂无候选，点击右上「从经验生成候选」
          </div>
        ) : (
          <div className="cand-list">
            {candidates.map((c) => (
              <CandidateCard key={c.id} candidate={c} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function CandidateCard({ candidate }: { candidate: SkillCandidate }) {
  const [open, setOpen] = useState(false)
  const evaluate = useEvaluateCandidate()
  const promote = usePromoteCandidate()
  const draft = candidate.draft_spec ?? {}
  const keys = ['prompt', 'steps', 'tools', 'io_schema'].filter((k) => k in draft)

  return (
    <div className="card cand-card">
      <div className="c-top">
        <span className="c-name">{candidate.name}</span>
        <span className={`badge ${candidate.status === 'promoted' ? 'emerald' : 'amber'}`}>
          {candidate.status}
        </span>
      </div>
      <div className="c-meta">
        <span>领域 {candidate.domain}</span>
        <span>相似度 {candidate.similarity_score.toFixed(2)}</span>
        <span>来源经验 {candidate.experience_ids.length} 条</span>
        <span>要素 {keys.length}/4</span>
      </div>
      <div className="c-actions">
        <button type="button" className="btn sm" onClick={() => setOpen((v) => !v)}>
          {open ? '收起草案' : '查看草案'}
        </button>
        <button
          type="button"
          className="btn sm"
          disabled={evaluate.isPending}
          onClick={() => evaluate.mutate({ candidateId: candidate.id })}
        >
          {evaluate.isPending ? '评估中…' : '运行评估'}
        </button>
        <button
          type="button"
          className="btn sm primary"
          disabled={promote.isPending}
          onClick={() => promote.mutate({ candidateId: candidate.id })}
        >
          {promote.isPending ? '固化中…' : '固化并发布'}
        </button>
      </div>
      {open && <pre className="spec-box">{JSON.stringify(draft, null, 2)}</pre>}
      {evaluate.isSuccess && (
        <p className="text-12" style={{ color: 'var(--emerald-4)' }}>
          评估完成 · 结论 {evaluate.data.verdict}
        </p>
      )}
      {promote.isError && (
        <p className="text-12" style={{ color: 'var(--danger-fg)' }}>
          固化失败：{(promote.error as Error)?.message}
        </p>
      )}
      {promote.isSuccess && (
        <p className="text-12" style={{ color: 'var(--emerald-4)' }}>
          已固化为 v{promote.data.semver}
        </p>
      )}
    </div>
  )
}
