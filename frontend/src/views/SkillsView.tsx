/**
 * SkillsView — 技能中心 (Skill Hub, PRD §6.1 / T17 + INC34).
 *
 * Shows the skill registry (search + domain filter + versions + rollback) and
 * the skill-candidate pipeline: 经验 → 编译候选 → 评估 → 固化(promote).
 *
 * INC34 新增（全部真调后端，不编造）：
 *   * 手工创建技能（`POST /skills`）；
 *   * 为技能新建版本（`POST /skills/{id}/versions`），`spec.io_schema` 用**可编辑
 *     JSON 文本框**：客户端做 JSON 语法校验，后端做结构校验，**后端 400 的中文
 *     原因逐字展示**；
 *   * 导出技能 JSON（`GET /skills/{id}/export`）。
 *
 * 权限口径：创建 / 编辑 / 导出属管理动作，仅 manager+ 渲染（编译 / 发布本身也需
 * write:skills / approve:skills）；浏览与候选流程保持既有行为不变。
 */

import { useState } from 'react'
import {
  useCompileCandidate,
  useCreateSkill,
  useCreateSkillVersion,
  useEvaluateCandidate,
  useExperiences,
  useHubSkills,
  usePromoteCandidate,
  useRollbackSkill,
  useSkillCandidates,
  useSkillVersions,
} from '../api/hooks'
import type { Skill, SkillCandidate, SkillVersion } from '../api/client'
import { downloadSkillExport } from '../api/client'
import { humanizeError } from '../api/errors'
import { useSession } from '../hooks/useSession'
import { roleAtLeast } from '../auth/roleGate'
import { IconChevronDown, IconSparkle } from '../components/icons'
import '../styles/skills.css'

const DOMAINS = ['', '数据分析', '企业知识库', '项目管理', '代码开发', 'general']
const CREATE_DOMAINS = ['general', '数据分析', '企业知识库', '项目管理', '代码开发']

export function SkillsView() {
  const [q, setQ] = useState('')
  const [domain, setDomain] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const skillsQ = useHubSkills({ q: q || undefined, domain: domain || undefined, limit: 60 })
  const skills = skillsQ.data?.items ?? []
  const selectedSkill = skills.find((s) => s.id === selected) ?? null

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

        {selectedSkill && <VersionPanel skill={selectedSkill} />}

        <CreateSkillPanel />

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

function VersionPanel({ skill }: { skill: Skill }) {
  const versionsQ = useSkillVersions(skill.id)
  const rollback = useRollbackSkill()
  const versions = versionsQ.data ?? []
  const [exportNote, setExportNote] = useState<string | null>(null)

  const onExport = async () => {
    setExportNote(null)
    try {
      await downloadSkillExport(skill.id, `${skill.name || skill.id}-export.json`)
      setExportNote('已导出技能 JSON')
    } catch (err) {
      setExportNote(humanizeError(err, '导出失败').label)
    }
  }

  return (
    <div className="panel" style={{ marginTop: 20 }}>
      <div className="panel-head">
        <div className="title">「{skill.name}」· 版本</div>
        <div className="actions">
          <span>{versionsQ.isLoading ? '加载中…' : `${versions.length} 个版本`}</span>
          <button type="button" className="btn sm" onClick={onExport}>
            导出 JSON
          </button>
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
                    rollback.mutate({ skillId: skill.id, toVersion: v.semver })
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
        {exportNote && (
          <p className="text-12" style={{ color: 'var(--fg-muted)', marginTop: 8 }} role="status">
            {exportNote}
          </p>
        )}

        {/* INC34 —— 新建版本 / 编辑 spec（含可编辑 io_schema JSON 文本框）。 */}
        <SkillVersionEditor
          key={`${skill.id}-${versions[0]?.id ?? 'none'}`}
          skillId={skill.id}
          latest={versions[0]}
        />
      </div>
    </div>
  )
}

function SkillVersionEditor({ skillId, latest }: { skillId: string; latest?: SkillVersion }) {
  const session = useSession()
  const canManage = roleAtLeast(session?.role, 'manager')
  const createVersion = useCreateSkillVersion()

  const initialSpec = (latest?.spec ?? {}) as Record<string, unknown>
  const [prompt, setPrompt] = useState(typeof initialSpec.prompt === 'string' ? initialSpec.prompt : '')
  const [stepsText, setStepsText] = useState(
    Array.isArray(initialSpec.steps) ? (initialSpec.steps as string[]).join('\n') : '',
  )
  const [toolsText, setToolsText] = useState(
    Array.isArray(initialSpec.tools) ? (initialSpec.tools as string[]).join(', ') : '',
  )
  const [ioText, setIoText] = useState(
    initialSpec.io_schema ? JSON.stringify(initialSpec.io_schema, null, 2) : '',
  )
  const [changelog, setChangelog] = useState('')
  const [bump, setBump] = useState('patch')
  const [ioError, setIoError] = useState<string | null>(null)

  if (!canManage) return null

  const versionErr = createVersion.isError
    ? humanizeError(createVersion.error, '创建版本失败')
    : null

  const onSubmit = () => {
    setIoError(null)
    const spec: Record<string, unknown> = {}
    if (prompt.trim()) spec.prompt = prompt.trim()
    const steps = stepsText.split('\n').map((s) => s.trim()).filter(Boolean)
    if (steps.length) spec.steps = steps
    const tools = toolsText.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
    if (tools.length) spec.tools = tools
    if (ioText.trim()) {
      try {
        spec.io_schema = JSON.parse(ioText)
      } catch (err) {
        setIoError(`IO 结构不是合法 JSON：${(err as Error).message}`)
        return
      }
    }
    createVersion.mutate({
      skillId,
      spec,
      changelog: changelog.trim() || undefined,
      bump,
    })
  }

  return (
    <details className="skill-editor" data-testid="skill-version-editor">
      <summary className="skill-editor-sum">
        编辑 / 新建版本
        <IconChevronDown className="chev" />
      </summary>
      <div className="skill-editor-body">
        <label className="skill-field">
          <span>提示词（prompt）</span>
          <textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </label>
        <label className="skill-field">
          <span>步骤（每行一步）</span>
          <textarea rows={3} value={stepsText} onChange={(e) => setStepsText(e.target.value)} />
        </label>
        <label className="skill-field">
          <span>工具（逗号或换行分隔）</span>
          <input value={toolsText} onChange={(e) => setToolsText(e.target.value)} />
        </label>
        <label className="skill-field">
          <span>IO 结构（io_schema，JSON）</span>
          <textarea
            rows={6}
            className="skill-io"
            value={ioText}
            onChange={(e) => setIoText(e.target.value)}
            placeholder={'{\n  "input": { "intent": "string" },\n  "output": { "summary": "string" }\n}'}
          />
        </label>
        {ioError && (
          <p className="text-12" style={{ color: 'var(--danger-fg)' }} role="alert">
            {ioError}
          </p>
        )}
        <div className="skill-editor-row">
          <label className="skill-field inline">
            <span>版本递增</span>
            <select value={bump} onChange={(e) => setBump(e.target.value)}>
              <option value="patch">修订（patch）</option>
              <option value="minor">次要（minor）</option>
              <option value="major">主要（major）</option>
            </select>
          </label>
          <label className="skill-field inline grow">
            <span>变更说明（可选）</span>
            <input value={changelog} onChange={(e) => setChangelog(e.target.value)} />
          </label>
        </div>
        <div className="skill-editor-row">
          <button
            type="button"
            className="btn sm primary"
            data-testid="skill-version-save"
            disabled={createVersion.isPending}
            onClick={onSubmit}
          >
            {createVersion.isPending ? '保存中…' : '保存新版本'}
          </button>
          {createVersion.isSuccess && (
            <span className="text-12" style={{ color: 'var(--emerald-fg)' }} role="status">
              已创建 v{createVersion.data.semver}
            </span>
          )}
        </div>
        {versionErr && (
          <p className="text-12" style={{ color: 'var(--danger-fg)' }} role="alert" title={versionErr.detail}>
            {versionErr.label}
          </p>
        )}
      </div>
    </details>
  )
}

function CreateSkillPanel() {
  const session = useSession()
  const canManage = roleAtLeast(session?.role, 'manager')
  const create = useCreateSkill()
  const [name, setName] = useState('')
  const [domain, setDomain] = useState('general')
  const [owner, setOwner] = useState('')
  const [description, setDescription] = useState('')

  if (!canManage) return null

  const err = create.isError ? humanizeError(create.error, '创建技能失败') : null

  const onSubmit = () => {
    if (!name.trim()) return
    create.mutate(
      { name: name.trim(), domain, owner: owner.trim() || undefined, description: description.trim() || undefined },
      {
        onSuccess: () => {
          setName('')
          setOwner('')
          setDescription('')
        },
      },
    )
  }

  return (
    <details className="skill-editor" data-testid="skill-create">
      <summary className="skill-editor-sum">
        新建技能
        <IconChevronDown className="chev" />
      </summary>
      <div className="skill-editor-body">
        <div className="skill-editor-row">
          <label className="skill-field inline grow">
            <span>名称</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：周报生成器" />
          </label>
          <label className="skill-field inline">
            <span>领域</span>
            <select value={domain} onChange={(e) => setDomain(e.target.value)}>
              {CREATE_DOMAINS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="skill-field">
          <span>负责人（可选，默认当前用户）</span>
          <input value={owner} onChange={(e) => setOwner(e.target.value)} />
        </label>
        <label className="skill-field">
          <span>描述（可选）</span>
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="skill-editor-row">
          <button
            type="button"
            className="btn sm primary"
            data-testid="skill-create-submit"
            disabled={create.isPending || !name.trim()}
            onClick={onSubmit}
          >
            {create.isPending ? '创建中…' : '创建技能'}
          </button>
          {create.isSuccess && (
            <span className="text-12" style={{ color: 'var(--emerald-fg)' }} role="status">
              已创建「{create.data.name}」
            </span>
          )}
        </div>
        {err && (
          <p className="text-12" style={{ color: 'var(--danger-fg)' }} role="alert" title={err.detail}>
            {err.label}
          </p>
        )}
      </div>
    </details>
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
        <div className="title">技能候选</div>
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
          <p className="text-12" style={{ color: 'var(--amber-fg)', marginBottom: 12 }}>
            编译未完成：{(compile.error as Error)?.message}
          </p>
        )}
        {compile.isSuccess && compile.data?.status === 'insufficient' && (
          <p className="text-12" style={{ color: 'var(--amber-fg)', marginBottom: 12 }}>
            相似经验不足：当前 {compile.data.experience_ids.length} 条
            {compile.data.required_experiences != null
              ? `，生成技能候选至少需要 ${compile.data.required_experiences} 条相似经验。`
              : '，暂无法固化为技能。'}
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
        <p className="text-12" style={{ color: 'var(--emerald-fg)' }}>
          评估完成 · 结论 {evaluate.data.verdict}
        </p>
      )}
      {promote.isError && (
        <p className="text-12" style={{ color: 'var(--danger-fg)' }}>
          固化失败：{(promote.error as Error)?.message}
        </p>
      )}
      {promote.isSuccess && (
        <p className="text-12" style={{ color: 'var(--emerald-fg)' }}>
          已固化为 v{promote.data.semver}
        </p>
      )}
    </div>
  )
}
