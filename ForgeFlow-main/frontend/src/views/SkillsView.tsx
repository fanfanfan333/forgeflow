/**
 * SkillsView — 技能资产中心 (Skill Asset Center, PRD §6.1 / T17 + INC34 + INC43 S2).
 *
 * INC43 S2（本文件）——把「技能中心」从平铺列表升级为**三栏资产中心**：
 *   SkillLibrary（左） | SkillEngineering（中） | SkillInspector（右），
 * 数据全部来自真实 API（`/skills`、`/skill-candidates`、`/skills/{id}/versions`），
 * 零新增后端端点。三栏口径由 `views/skills/skillAssets.ts` 的**纯函数**给出，
 * 本文件只做编排与交互。
 *
 * 既有能力（INC34）**全部保留**：手工创建技能（`POST /skills`）、新建版本
 * （`POST /skills/{id}/versions`，含可编辑 io_schema JSON 文本框，后端 400 中文原因
 * 逐字上屏）、导出技能 JSON、技能候选流水线（经验 → 编译 → 评估 → 固化）。
 * 这些子组件（`SkillTile`/`VersionPanel`/`SkillVersionEditor`/`CreateSkillPanel`/
 * `CandidateSection`/`CandidateCard`）与本文件原有 testid（`skill-create`、
 * `skill-create-submit`、`skill-version-editor`、`skill-version-save`）**原样保留**，
 * 下沉到三栏下方的「更多」区。
 *
 * 权限口径不变：创建 / 编辑 / 导出属管理动作，仅 manager+ 渲染。
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
import type { SkillScope, SkillSubject } from './skills/skillAssets'
import { filterByScope, groupSkillsByStatus } from './skills/skillAssets'
import { SkillLibrary } from './skills/SkillLibrary'
import { SkillEngineering } from './skills/SkillEngineering'
import { SkillInspector } from './skills/SkillInspector'
import '../styles/skills.css'
import '../styles/skill-assets.css'

const CREATE_DOMAINS = ['general', '数据分析', '企业知识库', '项目管理', '代码开发']

/** 版本面板在「更多」区的滚动锚点（供右栏「查看版本」定位）。 */
const VERSIONS_ANCHOR_ID = 'skill-assets-versions'

export function SkillsView() {
  const [q, setQ] = useState('')
  const [scope, setScope] = useState<SkillScope>('all')
  const [selected, setSelected] = useState<string | null>(null)
  const session = useSession()
  const selfUserId = session?.userId ?? null

  const skillsQ = useHubSkills({ q: q || undefined, limit: 60 })
  const allSkills = skillsQ.data?.items ?? []
  const scopedSkills = filterByScope(allSkills, scope, selfUserId)

  const candidatesQ = useSkillCandidates({ limit: 30 })
  const candidates = candidatesQ.data?.items ?? []

  const groups = groupSkillsByStatus(scopedSkills, candidates)

  // 选中主体：优先匹配可见技能，其次匹配候选（id 域不重叠）。
  const selectedSkill = scopedSkills.find((s) => s.id === selected) ?? null
  const selectedCandidate = selectedSkill
    ? null
    : (candidates.find((c) => c.id === selected) ?? null)

  const versionsQ = useSkillVersions(selectedSkill ? selectedSkill.id : null)
  const latestVersion = versionsQ.data?.[0]

  const subject: SkillSubject | null = selectedSkill
    ? { kind: 'skill', skill: selectedSkill, version: latestVersion }
    : selectedCandidate
      ? { kind: 'candidate', candidate: selectedCandidate }
      : null

  const showVersions = () => {
    if (typeof document === 'undefined') return
    document.getElementById(VERSIONS_ANCHOR_ID)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

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

      <div className="page-body">
        <div className="skill-assets-workspace" data-testid="skill-workspace">
          <SkillLibrary
            groups={groups}
            selectedId={selected}
            onSelect={setSelected}
            q={q}
            onQ={setQ}
            scope={scope}
            onScope={setScope}
            loading={skillsQ.isLoading}
            error={skillsQ.isError ? ((skillsQ.error as Error)?.message ?? '未知错误') : null}
          />
          <SkillEngineering subject={subject} />
          <SkillInspector subject={subject} onShowVersions={showVersions} />
        </div>

        {/* 「更多」区 —— 保留 INC34 既有子组件与全部 testid（下沉，行为不变）。 */}
        <div className="skill-assets-more">
          <div className="skill-assets-more-title">更多技能与沉淀</div>

          {skillsQ.isError ? (
            <div className="card empty">加载失败：{(skillsQ.error as Error)?.message}</div>
          ) : scopedSkills.length === 0 && !skillsQ.isLoading ? (
            <div className="card empty">
              <span className="big">
                <IconSparkle width={22} height={22} />
              </span>
              暂无技能，先从经验编译一个候选吧
            </div>
          ) : (
            <div className="skill-grid">
              {scopedSkills.map((s) => (
                <SkillTile
                  key={s.id}
                  skill={s}
                  selected={selected === s.id}
                  onSelect={() => setSelected(selected === s.id ? null : s.id)}
                />
              ))}
            </div>
          )}

          {selectedSkill && (
            <div id={VERSIONS_ANCHOR_ID}>
              <VersionPanel skill={selectedSkill} />
            </div>
          )}

          <CreateSkillPanel />

          <CandidateSection />
        </div>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------------- *
 * 以下子组件为 INC34 既有实现，**原样保留**（不改行为、不改 testid）。
 * ------------------------------------------------------------------------- */

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
