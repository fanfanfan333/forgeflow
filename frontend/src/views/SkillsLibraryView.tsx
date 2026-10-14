import { useEffect, useRef, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useHubSkills, useSkillCandidates, useSkillVersions, useSkillExperience, useSkillReadiness } from '../api/hooks'
import { hubApi, registerResource, downloadSkillExport } from '../api/client'
import { humanizeError } from '../api/errors'
import { useSession } from '../hooks/useSession'
import { roleAtLeast } from '../auth/roleGate'
import { DeleteSkill } from '../components/DeleteSkill'
import { CandidateReviewList } from '../components/CandidateReviewList'
import { ContextSyncStatus } from '../components/ContextSyncStatus'
import { IconSparkle, IconPlus, IconSearch } from '../components/icons'
import { ResultMarkdown } from './runs/ResultMarkdown'
import '../styles/skill-library.css'

const CLEANING = {
  name: '文档清洗', description: '整理文档中的多余空白、重复文字与格式问题，保留原始数据。',
  instructions: '# 文档清洗\n\n## 什么时候使用\n用户上传 Word、文本或表格，希望清理文字或规范内容时使用。\n\n## 处理方法\n1. 先读取上传文件，按真实文件类型选择工具，说明发现的问题。\n2. 清理行首行尾空白、连续的多余空格；只在有明确依据时修复重复或错误文字。\n3. 不修改数字、金额、日期、姓名或公式，不删除有效内容。不确定的修改先询问用户。\n4. 使用实际编辑工具生成文件，保存修改后的文件并返回下载结果。\n5. 核对修改后的内容，简要列出改动。文件没有问题时说明检查结果，不声称作了不存在的修改。',
  example: '清洗这份文档，整理多余空格，保留数字和原意，返回修改后的文件。',
}
type EditorValue = typeof CLEANING & { id?: string; sourceRunId?: string }
const EMPTY: EditorValue = { name: '', description: '', instructions: '', example: '' }

function useSkillDialog(onClose: () => void, busy = false) {
  const panel = useRef<HTMLElement>(null)
  useEffect(() => {
    const previous = document.activeElement
    panel.current?.querySelector<HTMLElement>('input, button')?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose()
      if (event.key !== 'Tab') return
      const items = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]') ?? [])
      if (!items.length) { event.preventDefault(); return }
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus() }
      else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus() }
    }
    window.addEventListener('keydown', key)
    return () => { window.removeEventListener('keydown', key); if (previous instanceof HTMLElement) previous.focus() }
  }, [onClose, busy])
  return panel
}

export function SkillsLibraryView() {
  const session = useSession()
  const canManage = roleAtLeast(session?.role, 'manager')
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'skills' | 'candidates'>('skills')
  const [editor, setEditor] = useState<EditorValue | null>(() => {
    const fromRun = new URLSearchParams(window.location.search).get('fromRun')
    return fromRun ? { ...EMPTY, sourceRunId: fromRun } : null
  })
  const [selected, setSelected] = useState<string | null>(null)
  const skills = useHubSkills({ q: search || undefined, limit: 100 })
  const candidates = useSkillCandidates({ limit: 100 })
  const packageInput = useRef<HTMLInputElement>(null)
  const cache = useQueryClient()
  const importPackage = useMutation({ mutationFn: hubApi.importSkillPackage, onSuccess: skill => {
    void cache.invalidateQueries({ queryKey: ['skills'] }); setSelected(skill.id); setTab('skills')
  } })
  const items = skills.data?.items ?? []
  return <section className="skills-library-page" data-testid="skills-library-page">
    <header className="skills-library-heading"><div><h1>技能</h1><p>把常用方法写成指令，让智能体按你的方式工作。</p></div>
      {canManage && <div className="skill-heading-actions"><input ref={packageInput} type="file" accept=".zip,.md" aria-label="选择技能包" hidden onChange={e => { const file = e.target.files?.[0]; if (file) importPackage.mutate(file); e.target.value = '' }} /><button disabled={importPackage.isPending} onClick={() => packageInput.current?.click()}>{importPackage.isPending ? '正在导入…' : '导入技能包'}</button><button className="skill-primary" onClick={() => setEditor({ ...EMPTY })}><IconPlus width={16} height={16} />新建技能</button></div>}
    </header>
    {importPackage.isError && <p role="alert">{humanizeError(importPackage.error, '技能包导入失败').label}</p>}
    <div className="skill-library-toolbar"><nav aria-label="技能分类"><button aria-pressed={tab === 'skills'} className={tab === 'skills' ? 'active' : ''} onClick={() => setTab('skills')}>技能库 <span>{items.length}</span></button>
      {canManage && <button aria-pressed={tab === 'candidates'} className={tab === 'candidates' ? 'active' : ''} onClick={() => setTab('candidates')}>待整理 <span>{candidates.data?.items?.length ?? '—'}</span></button>}</nav>
      <label><IconSearch width={16} height={16} /><input aria-label="搜索技能" placeholder="搜索技能" value={search} onChange={e => setSearch(e.target.value)} /></label>
    </div>
    {tab === 'skills' ? <>
      {skills.isLoading && <p className="skill-library-note">正在加载技能…</p>}
      {skills.isError && <p role="alert">{humanizeError(skills.error, '技能加载失败').label} <button onClick={() => void skills.refetch()}>重试</button></p>}
      {!items.length && !skills.isLoading && !skills.isError && <div className="skill-library-empty"><IconSparkle width={28} height={28} /><h2>{search ? '没有找到这个技能' : '从一个常用方法开始'}</h2><p>{search ? '试试其他关键词。' : '技能是一份可重复使用的工作说明。写清楚做什么、怎么做，以及应该交付什么。'}</p></div>}
      {!!items.length && <div className="skill-list-columns" aria-hidden="true"><span>技能名称</span><span>简要描述</span><span>状态</span><span>版本</span><span>操作</span></div>}
      <div className="instruction-skill-list" role="list" aria-label="已保存技能">{items.map(skill => <article className={`instruction-skill-row${selected === skill.id ? ' selected' : ''}`} key={skill.id} data-testid="instruction-skill-card" role="listitem">
        <button className="skill-row-name" onClick={() => setSelected(skill.id)}><span className="instruction-skill-icon"><IconSparkle width={21} height={21} /></span><span><h2>{skill.name}</h2><small>{skill.domain === 'general' ? '通用' : skill.domain}</small></span></button>
        <p>{skill.description || '打开使用说明，了解这个技能的用途。'}</p><span className={`skill-row-state${skill.status === 'published' ? ' ready' : ''}`}>{skill.status === 'published' ? '已就绪' : skill.status === 'retired' ? '已停用' : '待确认'}</span><span className="skill-row-version">{skill.current_version ? `v${skill.current_version}` : '暂无版本'}</span>
        <footer><button onClick={() => setSelected(skill.id)}>使用说明</button>{skill.status === 'published' && <Link to={'/skills/use/' + encodeURIComponent(skill.id)}>使用技能 ↗</Link>}<DeleteSkill id={skill.id} name={skill.name} onDeleted={() => setSelected(null)} /></footer>
      </article>)}</div>
      {canManage && <section className="skill-starter"><div><span>从示例开始</span><h2>文档清洗</h2><p>规范空白和文字，保留数字与原意，并返回处理后的文件。</p></div><button onClick={() => setEditor({ ...CLEANING })}>添加这个技能 <IconPlus width={16} height={16} /></button></section>}
    </> : <>
      <p className="skill-library-note">这些说明来自历史任务，尚未启用。确认用途、补充指令后，再保存为可使用的技能。</p>
      {candidates.isError && <p role="alert">{humanizeError(candidates.error, '候选加载失败').label}</p>}
      <CandidateReviewList items={candidates.data?.items ?? []} search={search} loading={candidates.isLoading} onOrganize={candidate => setEditor({ ...EMPTY, name: candidate.name.startsWith('generic') ? '' : candidate.name, description: typeof candidate.draft_spec?.description === 'string' ? candidate.draft_spec.description : '从历史任务整理的工作方法', instructions: String(candidate.draft_spec?.prompt || '') })} />
      {!candidates.isLoading && !candidates.data?.items?.length && <div className="skill-library-empty"><h2>暂时没有待整理的经验</h2><p>完成任务后，可以在高级管理中把经验整理成技能。</p></div>}
    </>}
    {canManage && <Link className="skill-advanced-link" to={'/skills/advanced' as string}>高级管理 · 版本、评估与经验沉淀 ↗</Link>}
    <ContextSyncStatus />
    {editor && <InstructionEditor initial={editor} onClose={() => setEditor(null)} />}
    {selected && <InstructionDetails id={selected} canManage={canManage} onEdit={value => { setSelected(null); setEditor(value) }} onClose={() => setSelected(null)} />}
  </section>
}

function InstructionEditor({ initial, onClose }: { initial: EditorValue; onClose: () => void }) {
  const [form, setForm] = useState(initial)
  const [goal, setGoal] = useState('')
  const sourceRun = useQuery({queryKey:['runs',initial.sourceRunId],queryFn:()=>hubApi.runDetail(initial.sourceRunId!),enabled:!!initial.sourceRunId,retry:false})
  const [resourceId, setResourceId] = useState<string | null>(null)
  const [trialId, setTrialId] = useState<string | null>(null)
  const generated = useMutation({ mutationFn: () => hubApi.draftInstructionSkill(goal), onSuccess: value => setForm(value) })
  const upload = useMutation({ mutationFn: (file: File) => registerResource({kind: 'file', file}), onSuccess: resource => { setResourceId(resource.id); setTrialId(null) } })
  const trial = useMutation({ mutationFn: () => hubApi.tryInstructionSkill(form, [resourceId!], form.example || form.description), onSuccess: handle => setTrialId(handle.run_id) })
  const trialResult = useQuery({queryKey: ['runs', trialId], queryFn: () => hubApi.runDetail(trialId!), enabled: !!trialId, refetchInterval: query => ['running','queued'].includes(query.state.data?.status || '') ? 1000 : false})
  const trialPassed = trialResult.data?.status === 'completed' && trialResult.data?.outcome === 'success'
  const change = (value: EditorValue) => { setForm(value); setTrialId(null) }
  const cache = useQueryClient()
  const create = useMutation({ mutationFn: () => form.id ? hubApi.updateInstructionSkill(form.id, form) : hubApi.createInstructionSkill({...form, trial_run_id: trialId}), onSuccess: () => { void cache.invalidateQueries({ queryKey: ['skills'] }); onClose() } })
  const panel = useSkillDialog(onClose, create.isPending)
  return <div className="skill-dialog-overlay"><section ref={panel} className="skill-dialog" role="dialog" aria-modal="true" aria-label={form.id ? '编辑指令技能' : '新建指令技能'}><header><div><h2>{form.id ? '编辑技能' : '新建技能'}</h2><p>一份清楚的工作说明，就是一个技能。</p></div><button aria-label="关闭技能编辑" onClick={onClose} disabled={create.isPending}>×</button></header>
    {!form.name && !form.instructions ? <div className="skill-natural-create"><h3>告诉智能体，你希望它学会什么。</h3>{sourceRun.data && <p>来源任务：{sourceRun.data.intent} <button onClick={()=>setGoal(`把这次任务的方法整理为可重复执行的技能：${sourceRun.data!.intent}`)}>使用这次任务的目标</button></p>}{sourceRun.isError && <p role="alert">来源任务无法读取，请直接描述技能目标。</p>}<textarea aria-label="希望它学会什么" rows={5} value={goal} onChange={e => setGoal(e.target.value)} placeholder="例如：清洗客户文档，去掉多余空格，保留金额和姓名，返回可下载文件。" />
      {generated.isError && <p role="alert">{humanizeError(generated.error, '草案生成失败').label}</p>}
      <button className="skill-primary" disabled={generated.isPending || goal.trim().length < 10} onClick={() => generated.mutate()}>{generated.isPending ? '正在生成草案…' : '生成技能草案'}</button></div> : <form onSubmit={e => { e.preventDefault(); if(form.id || trialPassed) create.mutate() }}>
      <label>名称<input disabled={create.isPending} required maxLength={80} value={form.name} onChange={e => change({ ...form, name: e.target.value })} placeholder="例如：文档清洗" /></label>
      <label>用途<input disabled={create.isPending} required maxLength={500} value={form.description} onChange={e => change({ ...form, description: e.target.value })} placeholder="这个技能能帮用户完成什么？" /></label>
      <label>使用指令<textarea disabled={create.isPending} required minLength={10} maxLength={12000} rows={8} value={form.instructions} onChange={e => change({ ...form, instructions: e.target.value })} placeholder="说明什么时候使用、处理步骤、需要保留的内容，以及输出要求。支持 Markdown。" /></label>
      <label>示例提问<input disabled={create.isPending} maxLength={500} value={form.example} onChange={e => change({ ...form, example: e.target.value })} placeholder="用户可以怎样发起任务？" /></label>
      {!form.id && <div className="skill-trial"><h3>用真实文件试运行</h3><label>试运行文件<input type="file" disabled={upload.isPending || trial.isPending || create.isPending} onChange={e => { const file = e.target.files?.[0]; if(file) upload.mutate(file) }} /></label>
        <button type="button" disabled={!resourceId || trial.isPending || create.isPending || (!!trialId && ['running','queued'].includes(trialResult.data?.status || 'running'))} onClick={() => trial.mutate()}>{trial.isPending ? '正在开始…' : '试运行'}</button>
        {(upload.isError || trial.isError || trialResult.isError) && <p role="alert">{humanizeError(upload.error || trial.error || trialResult.error, '试运行失败').label}</p>}
        {trialId && <p>{trialPassed ? '试运行通过，可以保存。' : ['running','queued'].includes(trialResult.data?.status || 'running') ? '正在运行，请等待结果…' : '试运行未通过，请检查结果并调整指令。'} <Link to={'/tasks/' + trialId}>查看结果、变更与验证 ↗</Link></p>}
      </div>}
      {create.isError && <p role="alert">{humanizeError(create.error, '保存失败').label}</p>}
      <footer><span>{form.id ? '更新会保留旧版本。' : '试运行通过后保存；这不等于全面质量认证。'}</span><button type="submit" className="skill-primary" disabled={create.isPending || (!form.id && !trialPassed)}>{create.isPending ? '正在保存…' : '保存技能'}</button></footer>
    </form>}
  </section></div>
}

function InstructionDetails({ id, onClose, onEdit, canManage }: { id: string; onClose: () => void; onEdit: (value: EditorValue) => void; canManage: boolean }) {
  const panel = useSkillDialog(onClose)
  const detail = useQuery({ queryKey: ['skills', 'instructions', id], queryFn: () => hubApi.instructionSkill(id), retry: false })
  const [tab, setTab] = useState<'instructions' | 'learning' | 'versions'>('instructions')
  const exportPackage = useMutation({ mutationFn: () => downloadSkillExport(id, `${detail.data?.name || 'skill'}.zip`, 'zip') })
  const download = () => {
    if (!detail.data) return
    const url = URL.createObjectURL(new Blob([detail.data.source], { type: 'text/markdown;charset=utf-8' }))
    const a = document.createElement('a'); a.href = url; a.download = 'SKILL.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return <div className="skill-dialog-overlay skill-details-overlay"><section ref={panel} className="skill-dialog skill-detail-drawer" role="dialog" aria-modal="true" aria-label="技能使用说明"><header><div><span className="skill-detail-kicker">技能 · v{detail.data?.version || '—'}</span><h2>{detail.data?.name || '技能说明'}</h2><p>{detail.data?.description}</p></div><button aria-label="关闭技能说明" onClick={onClose}>×</button></header>
    {detail.isPending && <p>正在读取说明…</p>}{detail.isError && <p role="alert">{humanizeError(detail.error, '说明加载失败').label}</p>}
    {detail.data && <><nav className="skill-detail-tabs" aria-label="技能详情"><button className={tab === 'instructions' ? 'active' : ''} onClick={() => setTab('instructions')}>使用说明</button><button className={tab === 'learning' ? 'active' : ''} onClick={() => setTab('learning')}>运行与学习</button><button className={tab === 'versions' ? 'active' : ''} onClick={() => setTab('versions')}>版本</button></nav>
      {tab === 'instructions' ? <><div className="instruction-skill-body"><ResultMarkdown source={detail.data.instructions || '此技能使用结构化流程，请在高级管理中查看。'} /></div>{detail.data.example && <div className="skill-example"><span>试着这样问</span><p>{detail.data.example}</p></div>}</> : <SkillLearning id={id} versionsOnly={tab === 'versions'} />}
      {detail.data.kind === 'skill_package' && <p className="skill-library-note">导入的技能包尚待评估，脚本未执行。附属资源：{detail.data.resources?.join('、') || '无'}。<button disabled={exportPackage.isPending} onClick={() => exportPackage.mutate()}>导出完整技能包</button></p>}{exportPackage.isError && <p role="alert">{humanizeError(exportPackage.error, '技能包导出失败').label}</p>}
      <footer><button onClick={download}>导出 SKILL.md</button>{canManage && detail.data.kind !== 'skill_package' && <button onClick={() => onEdit({ ...detail.data! })}>编辑指令</button>}{detail.data.status === 'published' && <Link className="skill-primary" to={'/skills/use/' + id}>使用这个技能 ↗</Link>}</footer></>}
  </section></div>
}

function SkillLearning({ id, versionsOnly }: { id: string; versionsOnly: boolean }) {
  const versions = useSkillVersions(id)
  const experiences = useSkillExperience(id)
  const readiness = useSkillReadiness(id)
  const events = useQuery({queryKey:['skills','learning-events',id],queryFn:()=>hubApi.skillLearningEvents(id)})
  const rollouts = useQuery({queryKey:['skills','rollouts',id],queryFn:()=>hubApi.skillRollouts(id)})
  const usage = useQuery({queryKey:['skills','usage',id],queryFn:()=>hubApi.skillUsage(id)})
  const generate = useMutation({mutationFn:()=>hubApi.forgeSkill({experience_ids: experiences.data?.items.map(e=>e.id)})})
  const canManage = roleAtLeast(useSession()?.role,'manager')
  return <div className="skill-learning" data-testid="skill-learning-panel">
    {[versions,experiences,readiness,events,rollouts,usage].some(q=>q.isError) && <p role="alert">部分记录读取失败，请稍后重试。缺失记录不代表已通过验证。</p>}
    {!versionsOnly && <><h3>使用表现</h3><dl className="skill-usage-facts"><dt>发起次数</dt><dd>{usage.data?.attempts ?? '—'}</dd><dt>任务成功率</dt><dd>{usage.data?.success_rate == null ? '等待已完成运行' : `${(usage.data.success_rate*100).toFixed(1)}%`}</dd><dt>平均耗时</dt><dd>{usage.data?.average_seconds == null ? '暂无测量' : `${usage.data.average_seconds.toFixed(1)} 秒`}</dd><dt>平均成本</dt><dd>当前不可计算</dd></dl><p>仅统计最近 {usage.data?.window_limit ?? '—'} 次运行中明确选择此技能的任务。</p>
      {usage.data?.runs.map(run=><p key={run.run_id}><Link to={'/tasks/'+run.run_id}>{run.intent}</Link> · {run.status}</p>)}
      <h3>这项技能有没有变强？</h3><p>当前版本评估：{readiness.data?.rate == null ? '暂无测量' : `${(readiness.data.rate*100).toFixed(1)}%`}。这是版本评估分数，不能当作用户任务成功率。</p>
      <h4>来源任务与经验</h4>{!experiences.data?.items.length && <p>尚无来自历史任务的学习证据。</p>}
      <ol>{experiences.data?.items.map(e=><li key={e.id}><Link to={'/tasks/'+e.run_id}>查看来源任务 ↗</Link><p>{e.summary}</p><small>{e.created_at}</small></li>)}</ol>
      {canManage && !!experiences.data?.items.length && <button disabled={generate.isPending} onClick={()=>generate.mutate()}>{generate.isPending ? '正在整理…' : '从这些经验生成改进候选'}</button>}
      {generate.isError && <p role="alert">{humanizeError(generate.error,'候选生成失败').label}</p>}
      {generate.data && <p>{generate.data.status === 'insufficient' ? '相似经验不足，尚未生成候选。' : '候选已生成，需评估和批准后才能生效。'} <Link to={'/skills/advanced' as string}>查看评估与发布 ↗</Link></p>}
      <h4>学习事件</h4>{!(events.data?.events?.length) && <p>尚未记录技能状态变化。</p>}<ol>{events.data?.events?.map((event,i)=><li key={i}><p>{String(event.reason || event.note || event.action || event.to_state || '技能状态变化')}</p><small>{String(event.created_at || event.at || '')}</small></li>)}</ol>
      <h4>验证与逐步发布</h4><p>{rollouts.data?.enabled ? '已启用逐步发布，以下为实际记录。' : '当前未启用逐步发布；不会自动声称技能已升级。'}</p>
      {rollouts.data?.rollouts.map((rollout,i)=><details key={i}><summary>{String(rollout.state || rollout.status || '发布记录')}</summary><pre>{JSON.stringify(rollout,null,2)}</pre></details>)}
    </>}
    <h3>版本演进</h3>{!versions.data?.length && <p>暂无版本记录。</p>}
    <ol>{versions.data?.map(v=><li key={v.id}><strong>v{v.semver}</strong><small> · {v.created_at}</small><p>{v.changelog || '未记录修改说明'}</p><p>评估：{v.eval_score == null ? '暂无测量' : String(v.eval_score)} · {v.approved_by ? '已人工批准' : '尚无批准记录'}</p>{typeof v.spec.trial_run_id === 'string' && <Link to={'/tasks/'+v.spec.trial_run_id}>查看试运行与验证 ↗</Link>}{!!v.source_experience_ids.length && <p>来源经验：{v.source_experience_ids.join('、')}</p>}</li>)}</ol>
  </div>
}
