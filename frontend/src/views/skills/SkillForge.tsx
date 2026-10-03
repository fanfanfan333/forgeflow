/**
 * SkillForge —— 技能「锻造」入口（INC46 T06）。
 *
 * 把真实经验**锻造**成一个技能候选：`POST /skills/forge`（复用后端既有
 * `candidate_compiler.compile_candidate`，无第二套草案逻辑），并可用
 * `GET /skills/forge/{id}`（`useForgeResult`）读回该次锻造记录。
 *
 * 诚实纪律（§8）：
 *   · 经验不足 ⇒ 后端返回诚实的 `status="insufficient"`（200）+ 真实阈值，
 *     界面逐字展示「需要至少 N 条」，**不伪造**候选；
 *   · 输入的经验 id 为空（manual 模式）时不发请求；
 *   · 失败经 `error` 原样上抛，绝不假装成功。
 */

import { useState } from 'react'
import { humanizeError } from '../../api/errors'
import { useForgeResult, useForgeSkill } from '../../api/hooks'

function parseIds(text: string): string[] {
  return text
    .split(/[\s,;\n]+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

export function SkillForge() {
  const forge = useForgeSkill()
  const [mode, setMode] = useState<'auto' | 'manual'>('auto')
  const [expText, setExpText] = useState('')
  const [readId, setReadId] = useState<string | null>(null)
  const readQ = useForgeResult(readId)

  const manualIds = parseIds(expText)
  const disabled = forge.isPending || (mode === 'manual' && manualIds.length === 0)

  const onSubmit = () => {
    setReadId(null)
    if (mode === 'manual') {
      forge.mutate({ mode: 'manual', experience_ids: manualIds })
    } else {
      forge.mutate({ mode: 'auto' })
    }
  }

  const result = forge.data
  const readResult = readId ? readQ.data : undefined
  const shown = readResult ?? result
  const err = forge.isError ? humanizeError(forge.error, '锻造失败') : null

  return (
    <div className="skill-forge" data-testid="skill-forge">
      <div className="skill-insights-block-title">技能锻造</div>

      <div className="skill-forge-controls">
        <label className="skill-forge-field inline">
          <span>模式</span>
          <select
            data-testid="skill-forge-mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as 'auto' | 'manual')}
          >
            <option value="auto">自动聚类相似经验</option>
            <option value="manual">指定经验（ids）</option>
          </select>
        </label>
        {mode === 'manual' && (
          <label className="skill-forge-field grow">
            <span>经验 ids（逗号 / 换行分隔）</span>
            <textarea
              rows={2}
              data-testid="skill-forge-exp"
              value={expText}
              onChange={(e) => setExpText(e.target.value)}
              placeholder="exp-1, exp-2"
            />
          </label>
        )}
        <button
          type="button"
          className="btn sm primary"
          data-testid="skill-forge-run"
          disabled={disabled}
          onClick={onSubmit}
        >
          {forge.isPending ? '锻造中…' : '锻造技能候选'}
        </button>
      </div>

      {err && (
        <p className="skill-insights-error" role="alert" title={err.detail}>
          {err.label}
        </p>
      )}

      {shown && (
        <div className="skill-forge-result" data-testid="skill-forge-result">
          <div className="skill-forge-result-row">
            <span className="text-mono">forge {shown.forge_id.slice(0, 18)}</span>
            <span
              className={`badge ${shown.status === 'compiled' ? 'emerald' : 'amber'}`}
            >
              {shown.status}
            </span>
          </div>
          {shown.status === 'insufficient' ? (
            <p className="skill-insights-empty">
              相似经验不足：当前 {shown.experience_ids.length} 条
              {shown.required_experiences != null
                ? `，生成候选至少需要 ${shown.required_experiences} 条。`
                : '，暂无法固化为技能。'}
            </p>
          ) : (
            <>
              <p className="skill-forge-name">{shown.name || '（未命名候选）'}</p>
              <div className="skill-forge-result-meta text-mono">
                <span>领域 {shown.domain || '—'}</span>
                <span>来源经验 {shown.experience_ids.length} 条</span>
                <span>相似度 {shown.similarity_score.toFixed(2)}</span>
                <span>候选 {shown.candidate_id ? shown.candidate_id.slice(0, 12) : '—'}</span>
              </div>
            </>
          )}
          <button
            type="button"
            className="btn sm"
            data-testid="skill-forge-read"
            disabled={readQ.isFetching}
            onClick={() => setReadId(shown.forge_id)}
          >
            {readQ.isFetching ? '读取中…' : '读取记录（GET）'}
          </button>
          {readId && readQ.isError && (
            <p className="skill-insights-error" role="alert">
              {humanizeError(readQ.error, '读取锻造记录失败').label}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
