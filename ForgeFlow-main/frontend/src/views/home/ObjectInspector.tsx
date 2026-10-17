/**
 * ObjectInspector — 工作台右栏「对象 Inspector」分派器（Phase B / T-B03 实现）。
 *
 * 按 `subject.kind` 分派到只读事实体（每个体是**独立组件**，只在自己的分支挂载并调用
 * 自己的 hook —— 避免条件 hook；AUDIT §14.1.4）：
 *   · run       ← `GET /runs/{id}`（`useRunDetail`）
 *   · artifact  ← `GET /runs/{id}`.artifacts[] + 既有 `ArtifactPreview`（预览）
 *   · skill     ← `GET /skills/resolve?ref=` + `GET /skills/{id}/readiness`
 *   · changeset ← `GET /runs/{id}`.codeplane（只读摘要；完整变更集由 `ChangeSetDrawer` 呈现）
 *   · memory    ← `GET /experiences?run_id=`（`useRunExperiences`）
 *
 * 诚实空态：无选中 ⇒「选择一个对象查看详情」；取数失败 / 无数据 ⇒「暂无数据」；
 * 无源字段（技能成功率 / 最近运行、数据集 Schema）⇒ 显式空态。
 *
 * `runId` 为**可选**后续字段：artifact / changeset / memory 的所属运行（来自左栏
 * 「最新 run」上下文）。这是对类图（§14.6 仅列 subject/onClose）的**必要补全** ——
 * `InspectObject` 契约（§14.3.2）不携带所属 run，故这三个 `kind` 必须有外部运行上下文。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { hubApi, previewResource } from '../../api/client'
import {
  useArtifactVersionChain,
  useResolvedSkill,
  useRunDetail,
  useRunExperiences,
  useSkillReadiness,
} from '../../api/hooks'
import { ArtifactPreview } from '../../components/ArtifactPreview'
import { InspectorSectionList } from './InspectorBlocks'
import { documentArtifactId } from './changeSet'
import {
  artifactSections,
  changeSetSections,
  memorySections,
  runSections,
  skillSections,
} from './inspectorSections'
import type { InspectObject } from './inspect'

type ObjectInspectorProps = {
  subject: InspectObject | null
  onClose: () => void
  /** artifact / changeset / memory 的所属运行（左栏「最新 run」）；缺省 ⇒ 对应体空态。 */
  runId?: string | null
  /**
   * Phase C / T-C01 —— 「代码变更集」分支的**打开抽屉**回执：右栏只出只读摘要，
   * 完整变更集（文件 / diff / validation / 审批 / 三动作）由工作台 `ChangeSetDrawer`
   * 呈现。缺省 ⇒ 按钮可见但无动作（本仓库始终由 HomeView 注入）。
   */
  onOpenChangeSet?: () => void
}

export function ObjectInspector(props: ObjectInspectorProps) {
  const { subject } = props

  if (!subject) {
    return (
      <section className="workbench-panel" data-testid="workbench-inspector-panel" aria-label="对象详情">
        <h2 className="workbench-panel-title">对象详情</h2>
        <p className="workbench-empty" data-testid="workbench-inspector-empty">
          选择一个对象查看详情
        </p>
      </section>
    )
  }

  return (
    <section className="workbench-panel" data-testid="workbench-inspector-panel" aria-label={`对象详情：${subject.label}`}>
      <header className="workbench-insp-head">
        <h2 className="workbench-panel-title">{subject.label}</h2>
        <button
          type="button"
          className="btn ghost sm"
          data-testid="workbench-inspector-close"
          aria-label="关闭对象详情"
          onClick={props.onClose}
        >
          关闭
        </button>
      </header>
      <InspectorBody
        subject={subject}
        runId={props.runId ?? null}
        onOpenChangeSet={props.onOpenChangeSet}
      />
    </section>
  )
}

/* ------------------------------------------------------------------------- *
 * 分派（按 kind）
 * ------------------------------------------------------------------------- */

function InspectorBody({
  subject,
  runId,
  onOpenChangeSet,
}: {
  subject: InspectObject
  runId: string | null
  onOpenChangeSet?: () => void
}) {
  switch (subject.kind) {
    case 'resource':
      return <ResourceBody resourceId={subject.id} runId={subject.runId ?? runId} />
    case 'run':
      return <RunBody runId={subject.id} />
    case 'artifact':
      return <ArtifactBody runId={runId} artifactId={subject.id} />
    case 'skill':
      return <SkillBody skillRef={subject.id} />
    case 'changeset':
      return <ChangeSetBody runId={runId} onOpen={onOpenChangeSet} />
    case 'memory':
      return <MemoryBody runId={runId} memoryId={subject.id} />
    default:
      return <Empty text="该对象暂无详情" />
  }
}

function ResourceBody({ resourceId, runId }: { resourceId: string; runId: string | null }) {
  const resource = useQuery({ queryKey: ['resources', resourceId], queryFn: () => hubApi.resource(resourceId) })
  const preview = useQuery({ queryKey: ['resource-preview', resourceId], queryFn: () => previewResource(resourceId), enabled: !!resource.data })
  if (resource.isPending) return <Loading />
  if (resource.isError) return <Empty text="文件读取失败，可能已删除或无权限" />
  const item = resource.data
  const facts = item.summary
  return <div className="dataset-inspector" data-testid="dataset-inspector"><h3>{item.name}</h3><dl>
    <dt>数据来源</dt><dd>用户上传 · {item.created_at}</dd>
    <dt>数据行 / 列</dt><dd>{facts.rows ?? '—'} / {facts.columns ?? '—'}</dd>
    <dt>字段</dt><dd>{facts.fields?.join('、') || '暂无字段信息'}</dd>
    <dt>缺失情况</dt><dd>{facts.quality?.empty_columns ? Object.entries(facts.quality.empty_columns as Record<string, number>).map(([key, value]) => `${key}：${(value * 100).toFixed(1)}%`).join('；') : '当前不可计算'}</dd>
    <dt>重复主键</dt><dd>{facts.quality?.duplicate_primary_key === true ? '发现重复' : facts.quality?.duplicate_primary_key === false ? '未发现重复' : '未指定主键，当前不可计算'}</dd>
    <dt>异常值 / 综合评分</dt><dd>暂无测量，不能据此认定数据无问题</dd>
  </dl>
    {preview.isError && <p role="alert">预览加载失败</p>}
    {preview.data?.available && (preview.data.format === 'table' ? <div style={{ overflowX: 'auto' }}><table><thead><tr>{preview.data.columns.map((name, i) => <th key={i}>{name}</th>)}</tr></thead><tbody>{preview.data.rows.map((row, i) => <tr key={i}>{row.map((value, j) => <td key={j}>{value}</td>)}</tr>)}</tbody></table></div> : <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{preview.data.content}</pre>)}
    {preview.data?.note && <p>{preview.data.note}</p>}
    {runId && <><Link to={'/tasks/' + runId}>查看本次分析与证据 ↗</Link><p><Link to={'/skills?fromRun=' + encodeURIComponent(runId)}>把这次方法整理为技能 ↗</Link></p></>}
  </div>
}

function RunBody({ runId }: { runId: string }) {
  const q = useRunDetail(runId)
  if (q.isLoading) return <Loading />
  if (q.isError || !q.data) return <Empty text="暂无数据" />
  return <InspectorSectionList sections={runSections(q.data)} />
}

function ArtifactBody({ runId, artifactId }: { runId: string | null; artifactId: string }) {
  const q = useRunDetail(runId)
  const [preview, setPreview] = useState(false)
  const sections = useMemo(
    () => (q.data ? artifactSections(q.data, artifactId) : []),
    [q.data, artifactId],
  )
  if (!runId) return <Empty text="暂无数据（缺少所属运行）" />
  if (q.isLoading) return <Loading />
  if (sections.length === 0) return <Empty text="本次无该产物" />
  return (
    <>
      <InspectorSectionList sections={sections} />
      <button
        type="button"
        className="btn ghost sm workbench-insp-action"
        data-testid="workbench-inspector-preview"
        onClick={() => setPreview(true)}
      >
        打开预览
      </button>
      {preview && <ArtifactPreview runId={runId} artifactId={artifactId} onClose={() => setPreview(false)} />}
    </>
  )
}

function SkillBody({ skillRef }: { skillRef: string }) {
  const skillsQ = useResolvedSkill(skillRef)
  const skill = skillsQ.data ?? null
  const readinessQ = useSkillReadiness(skill ? skill.id : null)
  if (skillsQ.isLoading) return <Loading />
  if (!skill) return <Empty text="暂无数据（未在技能目录中找到该技能）" />
  return <InspectorSectionList sections={skillSections(skill, readinessQ.data ?? null)} />
}

function ChangeSetBody({ runId, onOpen }: { runId: string | null; onOpen?: () => void }) {
  const q = useRunDetail(runId)
  // T-C03 —— 文档面需要版本链（`GET /artifacts/{id}/versions`）。`documentArtifactId`
  // 对代码面 / 无文档产物返回 `null` ⇒ 该 query `enabled:false`（代码面**零额外请求**）。
  const artifactId = useMemo(() => (q.data ? documentArtifactId(q.data) : null), [q.data])
  const chainQ = useArtifactVersionChain(artifactId)
  const sections = useMemo(
    () => (q.data ? changeSetSections(q.data, chainQ.data ?? null) : []),
    [q.data, chainQ.data],
  )
  if (!runId) return <Empty text="本次无代码变更（缺少所属运行）" />
  if (q.isLoading) return <Loading />
  if (sections.length === 0) return <Empty text="本次无代码变更" />
  return (
    <>
      <InspectorSectionList sections={sections} />
      {/* Phase C / T-C01 —— 右栏只出只读摘要；完整变更集由工作台抽屉呈现。 */}
      <button
        type="button"
        className="btn ghost sm workbench-insp-action"
        data-testid="workbench-inspector-open-changeset"
        onClick={onOpen}
      >
        打开 Change Set
      </button>
    </>
  )
}

function MemoryBody({ runId, memoryId }: { runId: string | null; memoryId: string }) {
  const q = useRunExperiences(runId)
  if (!runId) return <Empty text="本次无经验沉淀" />
  if (q.isLoading) return <Loading />
  const experiences = q.data?.items ?? []
  if (experiences.length === 0) return <Empty text="本次无经验沉淀" />
  const sections = memorySections(experiences)
  const referenced = experiences.some((experience) => experience.memory_ids.includes(memoryId))
  return (
    <>
      <InspectorSectionList sections={sections} />
      {!referenced && <Empty text="所选记忆未出现在本次经验中" />}
    </>
  )
}

/* ------------------------------------------------------------------------- *
 * 诚实空态小件（共享 testid：`workbench-inspector-empty`）
 * ------------------------------------------------------------------------- */

function Loading() {
  return (
    <p className="workbench-empty" data-testid="workbench-inspector-empty">
      加载中…
    </p>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <p className="workbench-empty" data-testid="workbench-inspector-empty">
      {text}
    </p>
  )
}
