import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ApiError, request } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { useHubSkills } from '../../api/hooks'
import { candidateKeys } from '../../api/queryKeys'

type Evaluation = {
  id: string
  dataset: string
  verdict: string
  metrics: { job_status?: string; ablation_enabled?: boolean; reason?: string; functional_measured?: boolean; quality_passed?: boolean; benefit?: { positive: boolean };
    cases?: { case_id: string; variant: string; passed: boolean; measured: boolean; trial_status?: string }[] }
}
type Dataset = { set_id: string; name: string; frozen: boolean; meets_initial_minimum: boolean }
const labels: Record<string, string> = { without_skill: '不使用技能', incumbent: '当前版本', candidate: '完整候选', instructions_only: '仅指令', preparation_only: '仅准备步骤' }

export function FunctionalSkillEvaluation({ candidateId }: { candidateId: string }) {
  const [open, setOpen] = useState(false)
  const [ablation, setAblation] = useState(false)
  const [dataset, setDataset] = useState('')
  const [incumbent, setIncumbent] = useState('')
  const cache = useQueryClient()
  const skills = useHubSkills({ limit: 100 })
  const datasets = useQuery({ queryKey: candidateKeys.datasets,
    queryFn: () => request<{ sets: Dataset[] }>('/eval/golden-sets'), enabled: open })
  const evaluation = useQuery({ queryKey: candidateKeys.evaluation(candidateId),
    queryFn: () => request<Evaluation>('/skill-candidates/' + candidateId + '/evaluation?kind=functional'),
    enabled: open, retry: false,
    refetchInterval: query => ['queued', 'running'].includes(query.state.data?.metrics.job_status ?? '') ? 2000 : false,
  })
  const start = useMutation({
    mutationFn: (resume: boolean) => request<Evaluation>('/skill-candidates/' + candidateId + '/functional-evaluate', {
      method: 'POST', body: JSON.stringify({ dataset: resume ? evaluation.data?.dataset : dataset,
        incumbent_skill_id: incumbent || undefined, include_ablation: resume ? evaluation.data?.metrics.ablation_enabled ?? false : ablation, resume_evaluation_id: resume ? evaluation.data?.id : undefined }),
    }),
    onSuccess: result => cache.setQueryData(candidateKeys.evaluation(candidateId), result),
  })
  const [instructions, setInstructions] = useState('')
  const [reason, setReason] = useState('')
  const revision = useMutation({ mutationFn: () => request<{ id: string }>('/skill-candidates/' + candidateId + '/revise', {
    method: 'POST', body: JSON.stringify({ instructions, reason }),
  }), onSuccess: () => { void cache.invalidateQueries({ queryKey: ['skill-candidates'] }) } })
  const rows = evaluation.data?.metrics.cases ?? []
  const running = start.isPending || ['queued', 'running'].includes(evaluation.data?.metrics.job_status ?? '')
  const frozen = datasets.data?.sets.filter(item => item.frozen && item.meets_initial_minimum) ?? []
  return <details className="skill-assets-insp-note" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>真实任务评测</summary>
    <p>用独立样例检查实际文件与引用，比较使用技能前后的结果。评测不会自动发布技能。</p>
    {datasets.isError && <p role="alert">{humanizeError(datasets.error).label}</p>}
    {datasets.data && !frozen.length ? <p>暂无已冻结的独立业务测试集。请由管理员先导入样例和验收规则。<Link to={"/evals" as string}>查看评测中心</Link></p> :
      <label>业务测试集<select aria-label="业务测试集" value={dataset} onChange={event => setDataset(event.target.value)}>
        <option value="">选择独立测试集</option>{frozen.map(item => <option value={item.set_id} key={item.set_id}>{item.name}</option>)}
      </select></label>}
    <label>对比已发布技能（可选）<select aria-label="对比已发布技能" value={incumbent} onChange={event => setIncumbent(event.target.value)}>
      <option value="">只比较使用前后</option>{skills.data?.items.filter(skill => skill.status === 'published').map(skill => <option key={skill.id} value={skill.id}>{skill.name}</option>)}
    </select></label>
    <label><input type="checkbox" checked={ablation} disabled={running} onChange={event => setAblation(event.target.checked)} />分别检查指令和准备步骤的收益（增加两组真实执行）</label>
    <button className="btn sm" type="button" disabled={!dataset || running} onClick={() => start.mutate(false)}>{running ? '评测中…' : '运行实际任务对照'}</button>
    {evaluation.data && ['failed', 'interrupted'].includes(evaluation.data.metrics.job_status ?? '') && rows.length > 0
      && rows.every(row => row.trial_status === 'completed') && <button className="btn sm" type="button" disabled={running}
        onClick={() => start.mutate(true)}>继续未完成的评测</button>}
    {start.error && <p role="alert">{humanizeError(start.error).label}</p>}
    {evaluation.error && !(evaluation.error instanceof ApiError && evaluation.error.status === 404) && <p role="alert">{humanizeError(evaluation.error).label}</p>}
    {evaluation.data?.metrics.reason && <p>{evaluation.data.metrics.reason}</p>}
    {evaluation.data?.metrics.quality_passed && evaluation.data.metrics.benefit?.positive === false && <p role="status">结果符合要求，但尚未证明技能比不使用技能或当前版本有额外收益，不能据此发布。</p>}
    {!!rows.length && <ul>{Object.entries(labels).map(([variant, label]) => {
      const group = rows.filter(row => row.variant === variant && row.trial_status !== 'running')
      return group.length ? <li key={variant}>{label}：通过 {group.filter(row => row.passed).length} / {group.length} 项</li> : null
    })}</ul>}
    {evaluation.data?.verdict === 'fail' && <details><summary>根据失败结果创建修订</summary>
      <p>保留原候选和失败证据，创建需要重新评测的新候选。</p>
      <label>修订后的完整指令<textarea aria-label="修订后的完整指令" minLength={10} maxLength={12000} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
      <label>修改原因<textarea aria-label="修改原因" minLength={5} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button type="button" disabled={revision.isPending || instructions.trim().length < 10 || reason.trim().length < 5} onClick={() => revision.mutate()}>创建修订候选</button>
      {revision.isError && <p role="alert">{humanizeError(revision.error).label}</p>}
      {revision.isSuccess && <p role="status">已创建修订，请在候选列表中选择新候选进行评测。</p>}
    </details>}
    {evaluation.data?.metrics.job_status === 'completed' && <p>{evaluation.data.verdict === 'pass' ? '本次功能评测通过，仍需人工发布。' : '本次功能评测未通过，候选保持未发布。'}</p>}
    {evaluation.data && evaluation.data.metrics.functional_measured === false && !running && <p>尚无完整的实际功能测量，结构检查结果不能代替实测。</p>}
  </details>
}
