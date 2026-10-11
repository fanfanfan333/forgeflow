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

import { useState } from 'react'
import {
  useDeleteSkill,
  useEvaluateCandidate,
  usePublishInterlock,
  usePromoteCandidate,
  useRestoreSkill,
  useRetireSkill,
  useSkillReadiness,
} from '../../api/hooks'
import { humanizeError } from '../../api/errors'
import { ConfirmDialog } from '../../components/ConfirmDialog'
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

      {/* INC46 T06 —— 就绪 / 发布联锁（仅技能主体有此真实端点）。 */}
      {subject.kind === 'skill' && <ReadinessBlock skillId={subject.skill.id} />}

      {/* INC54 §十一/§十二/§十三 —— 生命周期动作（退役 / 恢复 / 删除草稿）。 */}
      {subject.kind === 'skill' && (
        <SkillLifecycleActions skillId={subject.skill.id} status={subject.skill.status} />
      )}

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

/**
 * INC54 §十一/§十二/§十三 —— 技能生命周期动作（退役 / 恢复 / 删除草稿）。
 *
 * 用户诉求原文：「要为技能添加一个可以删除的按钮」+「（看不懂的 UUID）全部给为让人
 * 看懂的，否则删除」。落地时按**语义分层**，避免用户误以为点一下就永远没了：
 *   · 已发布 ⇒ 「退役 Skill」：软删（`status → retired`），可**恢复**；历史 Run /
 *     Evaluation / Experience / Artifact 的引用全部保留，只是不再被检索、不再作为
 *     新任务的默认技能（运行期只认 `published`）；
 *   · 已退役 ⇒ 「恢复发布」：`status → published`；
 *   · 草稿   ⇒ 「删除草稿」：**真删**（后端只允许 `draft`；已发布/被引用会被 409 拒绝）。
 *
 * 破坏性动作一律先过二次确认（复用既有 `ConfirmDialog`），后端真实错误（403 / 404 /
 * 409）经 `humanizeError` **逐字**呈现 —— **绝不吞错、绝不假装成功**。
 *
 * data-testid（只增）：`skill-inspector-lifecycle` / `skill-action-retire` /
 * `skill-action-restore` / `skill-action-delete`。
 */
function SkillLifecycleActions({ skillId, status }: { skillId: string; status: string }) {
  const retire = useRetireSkill()
  const restore = useRestoreSkill()
  const del = useDeleteSkill()
  const [confirm, setConfirm] = useState<'retire' | 'delete' | null>(null)

  const retired = status === 'retired'
  const draft = status === 'draft'
  const err = retire.isError
    ? humanizeError(retire.error, '退役失败')
    : restore.isError
      ? humanizeError(restore.error, '恢复失败')
      : del.isError
        ? humanizeError(del.error, '删除失败')
        : null

  return (
    <section className="skill-assets-insp-block" data-testid="skill-inspector-lifecycle">
      <h3 className="skill-assets-insp-title">生命周期</h3>
      <div className="skill-assets-insp-actions">
        {!draft && !retired && (
          <button
            type="button"
            className="btn sm"
            data-testid="skill-action-retire"
            onClick={() => setConfirm('retire')}
            disabled={retire.isPending}
            title="退役后不再被检索或选为新任务的默认技能；历史记录完好，可随时恢复"
          >
            {retire.isPending ? '退役中…' : '退役 Skill'}
          </button>
        )}
        {retired && (
          <button
            type="button"
            className="btn sm"
            data-testid="skill-action-restore"
            onClick={() => restore.mutate(skillId)}
            disabled={restore.isPending}
            title="恢复到已发布状态"
          >
            {restore.isPending ? '恢复中…' : '恢复发布'}
          </button>
        )}
        {draft && (
          <button
            type="button"
            className="btn sm danger"
            data-testid="skill-action-delete"
            onClick={() => setConfirm('delete')}
            disabled={del.isPending}
            title="只有草稿可物理删除；已发布技能请改用退役"
          >
            {del.isPending ? '删除中…' : '删除草稿'}
          </button>
        )}
      </div>

      {err && (
        <p className="skill-assets-insp-note danger" role="alert" title={err.detail}>
          {err.label}
        </p>
      )}

      <ConfirmDialog
        open={confirm === 'retire'}
        title="退役该技能？"
        body="退役后该技能不再被检索，也不会作为新任务的默认技能。历史运行、评估与产物不受影响，之后可以随时恢复发布。"
        confirmLabel="退役"
        cancelLabel="取消"
        danger
        busy={retire.isPending}
        onConfirm={() => {
          if (retire.isPending) return
          retire.mutate(skillId, { onSettled: () => setConfirm(null) })
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === 'delete'}
        title="删除该草稿？"
        body="草稿将被永久删除，且不可恢复。若该技能已被版本或评估引用，后端会拒绝删除。"
        confirmLabel="删除"
        cancelLabel="取消"
        danger
        busy={del.isPending}
        onConfirm={() => {
          if (del.isPending) return
          del.mutate(skillId, { onSettled: () => setConfirm(null) })
        }}
        onCancel={() => setConfirm(null)}
      />
    </section>
  )
}

/**
 * INC46 T06 —— 就绪 / 发布联锁（只读）。
 *
 * · `GET /skills/{id}/readiness`：就绪事实。`rate` **未测量 ⇒ null ⇒「—」**，
 *   绝不把「未测量」渲染成 `0%`（红线 4）；`evaluated` 区分「未测量」与「测得为 0」。
 * · `GET /evolution/interlock`（T15）：只读发布联锁状态；**无数据显示「—」**。
 */
function ReadinessBlock({ skillId }: { skillId: string }) {
  const readyQ = useSkillReadiness(skillId)
  const interlockQ = usePublishInterlock()
  const ready = readyQ.data
  const interlock = interlockQ.data
  const rate = ready?.rate
  return (
    <section className="skill-assets-insp-block" data-testid="skill-inspector-readiness">
      <h3 className="skill-assets-insp-title">就绪</h3>
      <p className="skill-assets-insp-eval text-mono" data-testid="skill-readiness-facts">
        就绪 {ready ? (ready.ready ? '是' : '否') : '—'}
        <span className="skill-assets-insp-dot" aria-hidden="true">
          ·
        </span>
        比率 {rate == null ? '—' : `${(rate * 100).toFixed(1)}%`}
        <span className="skill-assets-insp-dot" aria-hidden="true">
          ·
        </span>
        已测量 {ready ? ready.evaluated : '—'}
      </p>
      {readyQ.isError && (
        <p className="skill-assets-insp-note danger" role="alert">
          {humanizeError(readyQ.error, '就绪加载失败').label}
        </p>
      )}
      <div className="skill-assets-insp-eval text-mono" data-testid="skill-inspector-interlock">
        发布联锁{' '}
        {interlock
          ? interlock.released
            ? '已解除'
            : `未解除（缺 ${
                // INC46 T14 守卫：桩 / 降级响应可能缺 `missing`；缺失 ⇒「—」，
                // 真实数组 `[]` 仍按 0 计数（既有语义不变）。
                Array.isArray(interlock.missing) ? interlock.missing.length : '—'
              } 项）`
          : '—'}
        {interlock && (
          <span className="skill-assets-insp-note">
            {' '}
            · Level-1 {interlock.level1_open ? '开放' : '未开放'}
          </span>
        )}
      </div>
    </section>
  )
}
