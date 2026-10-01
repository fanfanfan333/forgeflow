/**
 * INC34 —— 结果区「沉淀为技能」（次级、克制、默认收起）。
 *
 * 把**一次运行的真实经验**一路走通到可复用技能资产：
 *   经验（`GET /experiences?run_id=`，运行结束时由 orchestrator 自动抽取）
 *     → 编译候选（`POST /skill-candidates`，mode="manual" 只取本次运行的经验）
 *     → 评估（`POST /skill-candidates/{id}/evaluate`）
 *     → 发布（`POST /skill-candidates/{id}/promote`，含治理门禁）。
 *
 * 诚实边界（P0-2）：
 *   * 无经验 / 阈值不足 / 无权限 / 失败，一律给**逐字中文**说明，绝不伪造候选或成功；
 *   * 「需要至少 N 条」里的 N 来自后端**配置**（`required_experiences`，源自
 *     `SKILL_CANDIDATE_MIN_EXPERIENCES`），前端**不写死、不臆造**；
 *   * 仅 manager+ 可见（编译/发布分别需要 write:skills / approve:skills），低权限
 *     角色**不渲染**该入口（不是禁用死按钮）。
 */

import { useState } from 'react'
import { humanizeError } from '../../api/errors'
import { useCompileCandidate, useEvaluateCandidate, usePromoteCandidate } from '../../api/hooks'
import { useSession } from '../../hooks/useSession'
import { roleAtLeast } from '../../auth/roleGate'
import { IconSparkle } from '../../components/icons'
import '../../styles/skill-capture.css'

export function SkillCapture({
  runId,
  hasExperience,
  pending,
  experienceIds,
}: {
  runId: string
  /** 本次运行是否真的抽取出了经验（`real.experience_id` 为真）。 */
  hasExperience: boolean
  /** 经验列表是否仍在加载。 */
  pending: boolean
  /** 本次运行的经验 id（来自已加载的 `/experiences?run_id=`，为空则不臆造）。 */
  experienceIds: string[]
}) {
  const session = useSession()
  const canManage = roleAtLeast(session?.role, 'manager')
  const [open, setOpen] = useState(false)
  const compile = useCompileCandidate()
  const evaluate = useEvaluateCandidate()
  const promote = usePromoteCandidate()

  // 低权限不渲染入口；无经验则无从沉淀（诚实，而非给一个点了会失败的按钮）。
  if (!canManage || !hasExperience) return null

  const candidate = compile.data && compile.data.status === 'draft' ? compile.data : null
  const insufficient = compile.data?.status === 'insufficient'
  const found = compile.data?.experience_ids.length ?? 0
  const required = compile.data?.required_experiences ?? null
  const evalPassed = evaluate.data?.verdict === 'pass'
  const spec = candidate?.draft_spec ?? {}
  const specKeys = ['prompt', 'steps', 'tools', 'io_schema'].filter((k) => k in spec)

  const compileErr = compile.isError ? humanizeError(compile.error, '生成技能候选失败') : null
  const evalErr = evaluate.isError ? humanizeError(evaluate.error, '评估失败') : null
  const promoteErr = promote.isError ? humanizeError(promote.error, '发布失败') : null

  return (
    <section className="res-capture" data-testid="result-capture-skill" aria-label="沉淀为技能">
      <button
        type="button"
        className="btn sm ghost res-capture-toggle"
        aria-expanded={open}
        title={`把运行 ${runId} 的执行经验沉淀为可复用技能`}
        onClick={() => setOpen((v) => !v)}
      >
        <IconSparkle width={14} height={14} />
        {open ? '收起' : '沉淀为技能'}
      </button>

      {open && (
        <div className="res-capture-panel" data-testid="result-capture-panel">
          <p className="res-capture-sub">
            把本次运行的执行经验固化为可复用技能：编译候选 → 评估 → 发布。
          </p>

          {pending ? (
            <p className="res-capture-note" data-testid="result-capture-note">
              正在准备本次运行的经验…
            </p>
          ) : experienceIds.length === 0 ? (
            <p className="res-capture-note" data-testid="result-capture-note">
              本次运行未找到可用于沉淀的经验。
            </p>
          ) : !compile.data && !compile.isPending ? (
            <div className="res-capture-row">
              <span className="res-capture-sub">
                将基于本次运行的 {experienceIds.length} 条经验生成技能候选
              </span>
              <button
                type="button"
                className="btn sm primary"
                data-testid="result-capture-compile"
                onClick={() => compile.mutate({ experience_ids: experienceIds, mode: 'manual' })}
              >
                生成技能候选
              </button>
            </div>
          ) : compile.isPending ? (
            <p className="res-capture-note" data-testid="result-capture-note">
              生成中…
            </p>
          ) : null}

          {compileErr && (
            <p className="res-capture-note err" role="alert" title={compileErr.detail}>
              {compileErr.label}
            </p>
          )}

          {insufficient && (
            <p className="res-capture-note" data-testid="result-capture-note" role="status">
              本次运行沉淀出 {found} 条经验；
              {required != null
                ? `生成技能候选至少需要 ${required} 条相似经验，暂无法封装。`
                : '相似经验不足，暂无法封装为技能。'}
              {' '}可在「技能中心」积累同类运行后再批量生成候选。
            </p>
          )}

          {candidate && (
            <div className="res-capture-cand">
              <div className="res-capture-cand-top">
                <span className="res-capture-cand-name">{candidate.name}</span>
                <span className="badge amber">{candidate.status}</span>
              </div>
              <div className="res-capture-cand-meta">
                <span>领域 {candidate.domain}</span>
                <span>来源经验 {candidate.experience_ids.length} 条</span>
                <span>要素 {specKeys.length}/4</span>
              </div>
              <div className="res-capture-row">
                <button
                  type="button"
                  className="btn sm"
                  data-testid="result-capture-evaluate"
                  disabled={evaluate.isPending}
                  onClick={() => evaluate.mutate({ candidateId: candidate.id })}
                >
                  {evaluate.isPending ? '评估中…' : '运行评估'}
                </button>
                <button
                  type="button"
                  className="btn sm primary"
                  data-testid="result-capture-promote"
                  disabled={promote.isPending || !evalPassed}
                  title={evalPassed ? undefined : '需先运行评估并通过'}
                  onClick={() => promote.mutate({ candidateId: candidate.id })}
                >
                  {promote.isPending ? '发布中…' : '发布为技能'}
                </button>
                {!evalPassed && (
                  <span className="res-capture-hint">需先运行评估并通过</span>
                )}
              </div>
              {evaluate.isSuccess && (
                <p className="res-capture-note" role="status">
                  评估完成 · 结论 {evaluate.data.verdict}
                </p>
              )}
              {evalErr && (
                <p className="res-capture-note err" role="alert" title={evalErr.detail}>
                  {evalErr.label}
                </p>
              )}
              {promote.isSuccess && (
                <p className="res-capture-note ok" role="status">
                  已发布为技能 v{promote.data.semver} ·{' '}
                  <a href="/skills" className="res-capture-link">
                    在技能中心查看
                  </a>
                </p>
              )}
              {promoteErr && (
                <p className="res-capture-note err" role="alert" title={promoteErr.detail}>
                  {promoteErr.label}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
