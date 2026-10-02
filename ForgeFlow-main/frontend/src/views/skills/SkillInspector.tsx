/**
 * SkillInspector — 技能资产中心**右栏**（INC43 S2）。
 *
 * 选中主体（技能或候选）的结构化事实（**只读**，不伪造流程、**不显示 runs**）：
 *   · `skill-inspector-basic`：名称 / 描述 / Owner / Version（缺失 ⇒「—」）；
 *   · `skill-inspector-capabilities`：能力 ✓ 列表（无 ⇒「—」）；
 *   · `skill-inspector-tools`：使用工具（无 ⇒「—」）；
 *   · `skill-inspector-eval`：评估得分 · 使用次数（无 ⇒「—」，**禁止编造成功率**）；
 *   · `skill-inspector-actions`：查看版本 / 运行测试 / 发布。
 *
 * 动作诚实口径：只有**真实存在**的后端能力才接线——
 *   · 「查看版本」→ 滚动到下方保留的版本面板（真实）；候选无版本 ⇒ 禁用；
 *   · 「运行测试」→ 候选走 `POST /skill-candidates/{id}/evaluate`；技能无对应端点 ⇒ 诚实禁用；
 *   · 「发布」    → 候选走 `POST /skill-candidates/{id}/promote`；技能无对应端点 ⇒ 诚实禁用。
 */

import { useEvaluateCandidate, usePromoteCandidate } from '../../api/hooks'
import { humanizeError } from '../../api/errors'
import type { InspectorFacts, SkillSubject } from './skillAssets'
import { candidateInspectorFacts, inspectorFacts } from './skillAssets'

type Props = {
  subject: SkillSubject | null
  onShowVersions: () => void
}

export function SkillInspector({ subject, onShowVersions }: Props) {
  return (
    <aside className="skill-assets-col skill-assets-inspector" data-testid="skill-inspector">
      {subject ? (
        <InspectorBody subject={subject} onShowVersions={onShowVersions} />
      ) : (
        <div className="skill-assets-empty">选择一个技能，查看它的检验详情</div>
      )}
    </aside>
  )
}

function InspectorBody({ subject, onShowVersions }: { subject: SkillSubject; onShowVersions: () => void }) {
  const evaluate = useEvaluateCandidate()
  const promote = usePromoteCandidate()
  const isCandidate = subject.kind === 'candidate'

  let facts: InspectorFacts
  let name: string
  let description: string
  let owner: string | null
  let version: string | null

  if (subject.kind === 'candidate') {
    facts = candidateInspectorFacts(subject.candidate)
    name = subject.candidate.name
    description = ''
    owner = null
    version = null
  } else {
    facts = inspectorFacts(subject.skill, subject.version)
    name = subject.skill.name
    description = subject.skill.description
    owner = subject.skill.owner
    version = subject.version?.semver ?? subject.skill.current_version
  }

  const evaluateErr = evaluate.isError ? humanizeError(evaluate.error, '运行测试失败') : null
  const promoteErr = promote.isError ? humanizeError(promote.error, '发布失败') : null

  const runTest = () => {
    if (subject.kind === 'candidate') evaluate.mutate({ candidateId: subject.candidate.id })
  }
  const runPublish = () => {
    if (subject.kind === 'candidate') promote.mutate({ candidateId: subject.candidate.id })
  }

  return (
    <>
      <section className="skill-assets-insp-block" data-testid="skill-inspector-basic">
        <h3 className="skill-assets-insp-title">基本信息</h3>
        <dl className="skill-assets-insp-facts">
          <div className="skill-assets-insp-row">
            <dt>名称</dt>
            <dd>{name}</dd>
          </div>
          <div className="skill-assets-insp-row">
            <dt>描述</dt>
            <dd>{description.trim() ? description : '—'}</dd>
          </div>
          <div className="skill-assets-insp-row">
            <dt>Owner</dt>
            <dd>{owner ?? '—'}</dd>
          </div>
          <div className="skill-assets-insp-row">
            <dt>Version</dt>
            <dd className="text-mono">{version ? `v${version}` : '—'}</dd>
          </div>
        </dl>
      </section>

      <section className="skill-assets-insp-block" data-testid="skill-inspector-capabilities">
        <h3 className="skill-assets-insp-title">能力</h3>
        {facts.capabilities.length > 0 ? (
          <ul className="skill-assets-insp-list">
            {facts.capabilities.map((cap) => (
              <li key={cap} className="skill-assets-insp-cap">
                <span aria-hidden="true">✓</span> {cap}
              </li>
            ))}
          </ul>
        ) : (
          <p className="skill-assets-insp-none">—</p>
        )}
      </section>

      <section className="skill-assets-insp-block" data-testid="skill-inspector-tools">
        <h3 className="skill-assets-insp-title">使用工具</h3>
        {facts.tools.length > 0 ? (
          <ul className="skill-assets-insp-list text-mono">
            {facts.tools.map((tool) => (
              <li key={tool}>{tool}</li>
            ))}
          </ul>
        ) : (
          <p className="skill-assets-insp-none">—</p>
        )}
      </section>

      <section className="skill-assets-insp-block" data-testid="skill-inspector-eval">
        <h3 className="skill-assets-insp-title">评估</h3>
        <p className="skill-assets-insp-eval text-mono">
          评估得分 {facts.evalScore == null ? '—' : facts.evalScore.toFixed(2)}
          <span className="skill-assets-insp-dot" aria-hidden="true">
            ·
          </span>
          使用次数 {facts.usageCount == null ? '—' : facts.usageCount}
        </p>
      </section>

      <section className="skill-assets-insp-block" data-testid="skill-inspector-actions">
        <div className="skill-assets-insp-actions">
          <button
            type="button"
            className="btn sm"
            data-testid="skill-action-versions"
            onClick={onShowVersions}
            disabled={isCandidate}
            title={isCandidate ? '候选尚无版本记录' : '查看版本'}
          >
            查看版本
          </button>
          <button
            type="button"
            className="btn sm"
            data-testid="skill-action-test"
            onClick={runTest}
            disabled={!isCandidate || evaluate.isPending}
            title={
              isCandidate
                ? '运行评估（真实调用）'
                : '评估接口将在后续版本提供（当前后端未提供技能级评估）'
            }
          >
            {evaluate.isPending ? '运行中…' : '运行测试'}
          </button>
          <button
            type="button"
            className="btn sm primary"
            data-testid="skill-action-publish"
            onClick={runPublish}
            disabled={!isCandidate || promote.isPending}
            title={
              isCandidate
                ? '固化并发布（真实调用）'
                : '发布接口将在后续版本提供（当前后端未提供技能级发布）'
            }
          >
            {promote.isPending ? '发布中…' : '发布'}
          </button>
        </div>

        {evaluate.isSuccess && isCandidate && (
          <p className="skill-assets-insp-note" role="status">
            评估完成 · 结论 {evaluate.data.verdict}
          </p>
        )}
        {evaluateErr && (
          <p className="skill-assets-insp-note danger" role="alert" title={evaluateErr.detail}>
            {evaluateErr.label}
          </p>
        )}
        {promote.isSuccess && isCandidate && (
          <p className="skill-assets-insp-note" role="status">
            已固化为 v{promote.data.semver}
          </p>
        )}
        {promoteErr && (
          <p className="skill-assets-insp-note danger" role="alert" title={promoteErr.detail}>
            {promoteErr.label}
          </p>
        )}
      </section>
    </>
  )
}
