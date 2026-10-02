/**
 * SkillEngineering — 技能资产中心**中栏**（INC43 S2 + S3 / T03）。
 *
 * 选中主体（技能或候选）的工程契约卡片：
 *   · `skill-card-header`：名称 + `vX.Y` + 状态徽标；
 *   · `skill-card-goal`：Goal（真实 `spec.goal` → `spec.prompt` →「—」）；
 *   · `skill-exec-flow` + `skill-exec-node`：执行节点链（`execFlowNodes`）；
 *   · `skill-card-counts`：`Tools / Policies / Tests` 三计数（缺失 ⇒「—」）。
 *
 * INC43 S3 / T03 —— 在上述**既有**卡片**下方**新增「工程闭环」区段（`skill-eng-*`），
 * 把后端 `forgeflow/api/routers/skill_engineering.py` 的闭环接到 UI，消除「已声明但
 * 不可达」的孤儿能力：
 *   · `skill-eng-lifecycle`：六态 `lifecycle` 徽标 + `next_states` 合法迁移 +
 *     `requires_approval` 时**指向 `/approvals` 的链接**（P1-5）；
 *   · `skill-eng-critique`：`severity` + `findings[]` + `must_fix[]`（阻断项逐字；
 *     空 ⇒「无阻断项」真实空态）；
 *   · `skill-eng-tests`：四类 `category` 计数（`skill-eng-test-cat` + `data-category`）
 *     + 逐条 `test_cases`/`test_runs` 的 `verdict`；**无用例 ⇒「—」**；
 *   · `skill-eng-eval`：`pass_rate` / `verified_pass_rate` / `sample_size` /
 *     `failure_modes[]`；`sample_size===0` ⇒ 一律「—」（**严禁** `0%`）；
 *   · `skill-eng-degraded`：`degraded_reason` 非空时逐字展示（诚实降级）；
 *   · `skill-eng-run`：**仅候选**可用的「运行工程闭环」按钮
 *     （`POST /skill-candidates/{id}/engineering`，**不含发布**）。
 *
 * 数据来源（**零编造**）：`useSkillEngineering`（技能或候选）+ `useSkillLifecycle`
 * （仅技能）。加载中 / 失败都有诚实态，绝不渲染假的 0 / 假成功率。既有 testid 与
 * 内容**一字不动**（红线：REMOVED=0）。
 */

import { humanizeError } from '../../api/errors'
import {
  useRunCandidateEngineering,
  useSkillEngineering,
  useSkillLifecycle,
} from '../../api/hooks'
import type { EngineeringSubject } from '../../api/hooks'
import type { SkillEngineeringResponse } from '../../api/client'
import type { SkillSubject } from './skillAssets'
import { execFlowNodes, skillCounts, skillGoal, skillStatusLabel, skillStatusTone } from './skillAssets'

/** 计数展示：存在 ⇒ 数字；缺失 ⇒ 诚实「—」（**非 0**）。 */
function countText(value: number | null): string {
  return value == null ? '—' : String(value)
}

export function SkillEngineering({ subject }: { subject: SkillSubject | null }) {
  if (!subject) {
    return (
      <section className="skill-assets-col skill-assets-engineering" data-testid="skill-engineering">
        <div className="skill-assets-empty">从左侧选择一个技能，查看它的工程契约</div>
      </section>
    )
  }

  let name: string
  let status: string
  let spec: Record<string, unknown> | undefined
  let version: string | null
  let badgeLabel: string
  let tone: string

  if (subject.kind === 'candidate') {
    name = subject.candidate.name
    status = subject.candidate.status
    spec = subject.candidate.draft_spec
    version = null
    // 候选：沿用后端 CANDIDATE_STATUSES（不翻译）。
    badgeLabel = status
    tone = ''
  } else {
    name = subject.skill.name
    status = subject.skill.status
    spec = subject.version?.spec
    version = subject.version?.semver ?? subject.skill.current_version
    badgeLabel = skillStatusLabel(status)
    tone = skillStatusTone(status)
  }

  const nodes = execFlowNodes(spec)
  // `declared` 取决于首个节点：来自 `spec.steps` 则是已声明契约，否则是前端骨架。
  const declared = nodes.length > 0 && nodes[0].declared
  const counts = skillCounts(spec)
  const goal = skillGoal(spec)

  return (
    <section className="skill-assets-col skill-assets-engineering" data-testid="skill-engineering">
      <article className="card skill-assets-card">
        <header className="skill-assets-card-head" data-testid="skill-card-header">
          <h2 className="skill-assets-card-name">{name}</h2>
          <span className="skill-assets-card-version text-mono">{version ? `v${version}` : '—'}</span>
          <span className={`badge ${tone}`}>{badgeLabel}</span>
        </header>

        <div className="skill-assets-card-goal" data-testid="skill-card-goal">
          <span className="skill-assets-card-goal-label">Goal</span>
          <p className="skill-assets-card-goal-text">{goal ?? '—'}</p>
        </div>

        <div className="skill-assets-flow" data-testid="skill-exec-flow">
          <div className="skill-assets-flow-title">Execution Flow</div>
          <ol className="skill-assets-flow-chain">
            {nodes.map((node) => (
              <li
                key={node.key}
                className="skill-assets-flow-node"
                data-testid="skill-exec-node"
                data-declared={node.declared ? 'true' : 'false'}
                title={node.declared ? node.label : '骨架（技能未声明步骤）'}
              >
                {node.label}
              </li>
            ))}
          </ol>
          {!declared && <p className="skill-assets-flow-note">骨架（技能未声明步骤）</p>}
        </div>

        <div className="skill-assets-counts text-mono" data-testid="skill-card-counts">
          <span className="skill-assets-count">Tools {countText(counts.tools)} tools</span>
          <span className="skill-assets-count-sep" aria-hidden="true">
            ·
          </span>
          <span className="skill-assets-count">Policies {countText(counts.policies)} rules</span>
          <span className="skill-assets-count-sep" aria-hidden="true">
            ·
          </span>
          <span className="skill-assets-count">Tests {countText(counts.tests)} cases</span>
        </div>
      </article>

      {/* INC43 S3 / T03 —— 工程闭环区段（把后端闭环接到 UI）。 */}
      <SkillEngineeringPanel subject={subject} />
    </section>
  )
}

/* ------------------------------------------------------------------------- *
 * INC43 S3 / T03 —— 工程闭环面板
 * ------------------------------------------------------------------------- */

/** 四类测试用例（顺序稳定，与后端 `SkillTestCase.category` 同域）。 */
const TEST_CATEGORIES = ['normal', 'boundary', 'adversarial', 'security'] as const

/** 比率展示：真实有限数 ⇒ 百分号格式；缺失 / 非法 ⇒「—」。**不做二次计算**。 */
function pctText(rate: number | null | undefined): string {
  return typeof rate === 'number' && Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : '—'
}

/** 一条 `findings[]` 元素的可读文本（未知形状 ⇒ 保真 JSON，绝不丢弃信息）。 */
function findingText(f: unknown): string {
  if (typeof f === 'string') return f
  if (f && typeof f === 'object') {
    const o = f as Record<string, unknown>
    for (const key of ['message', 'detail', 'text', 'title', 'code']) {
      const v = o[key]
      if (typeof v === 'string' && v.trim()) return v
    }
    return JSON.stringify(f)
  }
  return String(f)
}

function SkillEngineeringPanel({ subject }: { subject: SkillSubject }) {
  const engineeringSubject: EngineeringSubject =
    subject.kind === 'skill'
      ? { kind: 'skill', id: subject.skill.id }
      : { kind: 'candidate', id: subject.candidate.id }

  const eng = useSkillEngineering(engineeringSubject)
  const skillId = subject.kind === 'skill' ? subject.skill.id : null
  // 技能走专用 `/lifecycle` 端点；候选无该端点 ⇒ 回落工程响应携带的生命周期字段。
  const lifecycleQ = useSkillLifecycle(skillId)
  const runLoop = useRunCandidateEngineering()

  if (eng.isLoading) {
    return (
      <div className="skill-eng-panel" data-testid="skill-eng-panel">
        <p className="skill-eng-loading" data-testid="skill-eng-loading">
          工程闭环加载中…
        </p>
      </div>
    )
  }

  if (eng.isError) {
    return (
      <div className="skill-eng-panel" data-testid="skill-eng-panel">
        <p className="skill-eng-error" data-testid="skill-eng-error" role="alert">
          {humanizeError(eng.error, '工程事实加载失败').label}
        </p>
      </div>
    )
  }

  const data = eng.data
  if (!data) {
    return (
      <div className="skill-eng-panel" data-testid="skill-eng-panel">
        <p className="skill-eng-subtle">暂无工程数据</p>
      </div>
    )
  }

  return (
    <div className="skill-eng-panel" data-testid="skill-eng-panel">
      <LifecycleBlock data={data} lifecycle={lifecycleQ.data} />
      <CritiqueBlock data={data} />
      <TestsBlock data={data} />
      <EvaluationBlock data={data} />
      {data.degraded_reason && (
        <div className="skill-eng-degraded" data-testid="skill-eng-degraded">
          降级原因：{data.degraded_reason}
        </div>
      )}
      {subject.kind === 'candidate' && (
        <div className="skill-eng-run-block">
          <button
            type="button"
            className="btn sm primary"
            data-testid="skill-eng-run"
            disabled={runLoop.isPending}
            onClick={() => runLoop.mutate(subject.candidate.id)}
          >
            {runLoop.isPending ? '运行中…' : '运行工程闭环'}
          </button>
          {runLoop.isError && (
            <span className="skill-eng-error" role="alert">
              {humanizeError(runLoop.error, '工程闭环运行失败').label}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function LifecycleBlock({
  data,
  lifecycle,
}: {
  data: SkillEngineeringResponse
  lifecycle: { lifecycle: string; next_states: string[]; requires_approval: boolean } | undefined
}) {
  // 技能：以专用 `/lifecycle` 端点为准；候选：回落工程响应。两者都缺失 ⇒ 诚实「未知」。
  const state = lifecycle?.lifecycle ?? data.lifecycle
  const nextStates = lifecycle?.next_states ?? data.next_states ?? []
  const requiresApproval = lifecycle?.requires_approval ?? data.requires_approval ?? false
  return (
    <div className="skill-eng-block">
      <div className="skill-eng-block-title">生命周期</div>
      <div className="skill-eng-lifecycle" data-testid="skill-eng-lifecycle">
        <span className="badge">{state || '未知'}</span>
        {nextStates.length > 0 ? (
          <span className="skill-eng-next">可迁移至：{nextStates.join('、')}</span>
        ) : (
          <span className="skill-eng-subtle">无合法迁移</span>
        )}
        {requiresApproval && (
          <a className="skill-eng-approval" href="/approvals">
            需审批（HITL）· 前往审批 →
          </a>
        )}
      </div>
    </div>
  )
}

function CritiqueBlock({ data }: { data: SkillEngineeringResponse }) {
  const critique = data.critique
  const findings = critique?.findings ?? []
  const mustFix = critique?.must_fix ?? []
  return (
    <div className="skill-eng-block">
      <div className="skill-eng-block-title">独立评审</div>
      <div className="skill-eng-critique" data-testid="skill-eng-critique">
        <span className="skill-eng-sev text-mono">severity {critique?.severity ?? '未知'}</span>
        {findings.length > 0 ? (
          <ul className="skill-eng-findings">
            {findings.map((f, i) => (
              <li key={i}>{findingText(f)}</li>
            ))}
          </ul>
        ) : (
          <span className="skill-eng-subtle">无评审发现</span>
        )}
        {mustFix.length > 0 ? (
          <ul className="skill-eng-mustfix">
            {mustFix.map((m, i) => (
              <li key={i}>阻断项：{m}</li>
            ))}
          </ul>
        ) : (
          <span className="skill-eng-ok">无阻断项</span>
        )}
      </div>
    </div>
  )
}

function TestsBlock({ data }: { data: SkillEngineeringResponse }) {
  const cases = data.test_cases ?? []
  const runs = data.test_runs ?? []
  const runByCase = new Map(runs.map((r) => [r.case_id, r]))
  const hasCases = cases.length > 0
  const categoryCount = (category: string) =>
    cases.filter((c) => c.category === category).length
  return (
    <div className="skill-eng-block">
      <div className="skill-eng-block-title">测试用例</div>
      <div className="skill-eng-tests" data-testid="skill-eng-tests">
        <div className="skill-eng-cats text-mono">
          {TEST_CATEGORIES.map((category) => (
            <span
              key={category}
              className="skill-eng-test-cat"
              data-testid="skill-eng-test-cat"
              data-category={category}
            >
              {category} {hasCases ? categoryCount(category) : '—'}
            </span>
          ))}
        </div>
        {hasCases ? (
          <ul className="skill-eng-case-list">
            {cases.map((c) => {
              const run = runByCase.get(c.id)
              return (
                <li className="skill-eng-case" key={c.id}>
                  <span className="skill-eng-case-id text-mono" title={c.expectation}>
                    {c.id}
                  </span>
                  <span className={`skill-eng-verdict v-${run?.verdict ?? 'none'}`}>
                    {run ? run.verdict : '—'}
                  </span>
                </li>
              )
            })}
          </ul>
        ) : (
          <span className="skill-eng-subtle">无测试用例</span>
        )}
      </div>
    </div>
  )
}

function EvaluationBlock({ data }: { data: SkillEngineeringResponse }) {
  const evaluation = data.evaluation
  const sampleSize = evaluation?.sample_size
  // 未测量（缺失 / 0 样本）⇒ 比率一律「—」；**严禁**把「未测量」渲染成 `0%`。
  const measured = typeof sampleSize === 'number' && sampleSize > 0
  return (
    <div className="skill-eng-block">
      <div className="skill-eng-block-title">沙箱评估</div>
      <div className="skill-eng-eval" data-testid="skill-eng-eval">
        <span className="skill-eng-metric">
          通过率 {measured ? pctText(evaluation?.pass_rate) : '—'}
        </span>
        <span className="skill-eng-metric">
          已验证通过率 {measured ? pctText(evaluation?.verified_pass_rate) : '—'}
        </span>
        <span className="skill-eng-metric">
          样本 {typeof sampleSize === 'number' ? sampleSize : '—'}
        </span>
        {(evaluation?.failure_modes ?? []).length > 0 && (
          <span className="skill-eng-failmodes">
            失败模式：{(evaluation?.failure_modes ?? []).join('、')}
          </span>
        )}
      </div>
    </div>
  )
}
