/**
 * inspectorSections.ts — 右栏 Inspector 的**分段装配层**（Phase B / T-B03，纯函数）。
 *
 * 只做一件事：把 `inspect.ts` 的只读事实函数结果，按对象类型包成「分段」（标题 + 事实），
 * 供 `InspectorBlocks` 渲染。**不含 React、不发请求、不新增判定**；事实一律来自既有
 * 派生函数（`realRun.ts`）——本文件只是排版分组。
 *
 * 诚实纪律：空事实 ⇒ 返回 `[]`（由 `ObjectInspector` 渲染「暂无数据」）；**绝不**补 0。
 */
import type {
  Experience,
  RunDetail,
  Skill,
  SkillReadinessResponse,
  VersionChainResponse,
} from '../../api/client'
import { fmtTime } from '../runs/realRun'
import {
  artifactInspectFacts,
  changeSetInspectFacts,
  nonEmpty,
  runInspectFacts,
  skillInspectFacts,
} from './inspect'
import type { InspectFact } from './inspect'
import { deriveChangeSet } from './changeSet'

/** 一个只读事实段（标题 + 事实清单）。 */
export type InspectorSection = { key: string; title: string; facts: InspectFact[] }

/** Run 事实段（`GET /runs/{id}`）。 */
export function runSections(detail: RunDetail): InspectorSection[] {
  return [{ key: 'run', title: '运行', facts: runInspectFacts(detail) }]
}

/** Artifact 事实段（`GET /runs/{id}`.artifacts[]）；找不到该产物 ⇒ `[]`。 */
export function artifactSections(detail: RunDetail, id: string): InspectorSection[] {
  const facts = artifactInspectFacts(detail, id)
  if (facts.length === 0) return []
  return [{ key: 'artifact', title: '产物', facts }]
}

/** Skill 事实段（`GET /skills/resolve` + `GET /skills/{id}/readiness`）。 */
export function skillSections(
  skill: Skill,
  readiness: SkillReadinessResponse | null | undefined,
): InspectorSection[] {
  return [{ key: 'skill', title: '技能', facts: skillInspectFacts(skill, readiness) }]
}

/**
 * ChangeSet 事实段（代码面 `GET /runs/{id}`.codeplane，或文档面 + `chain`）；两面皆无 ⇒ `[]`。
 *
 * `chain`（`GET /artifacts/{id}/versions`）仅文档面需要（`version_registered` 行）；代码面
 * / 无文档产物 ⇒ 传 `null`（诚实的「今日无版本记录」，不报错）。
 */
export function changeSetSections(
  detail: RunDetail,
  chain: VersionChainResponse | null = null,
): InspectorSection[] {
  const facts = changeSetInspectFacts(detail, chain)
  if (facts.length === 0) return []
  const title = deriveChangeSet(detail, chain)?.source === 'document' ? '文档变更集' : '代码变更集'
  return [
    {
      key: 'changeset',
      title,
      // 说明行：右栏仅出**只读摘要**；完整变更集（文件 / diff / validation / 审批）
      // 由工作台「打开 Change Set」抽屉呈现（Phase C / T-C01）。
      facts: [...facts, { key: 'note', label: '说明', value: '只读摘要；完整变更集见「打开 Change Set」' }],
    },
  ]
}

/**
 * Memory / Experience 事实段（`GET /experiences?run_id=`）。
 *
 * 每条经验列出：摘要 / 结论 / 标签 / 时间（皆为后端真实字段，缺失 ⇒「—」）。
 * 无经验 ⇒ `[]`（由 UI 显示「本次无经验沉淀」）。
 */
export function memorySections(experiences: Experience[]): InspectorSection[] {
  if (experiences.length === 0) return []
  const facts: InspectFact[] = []
  experiences.forEach((experience, index) => {
    const n = index + 1
    facts.push({ key: `exp-${experience.id}-summary`, label: `经验 ${n} · 摘要`, value: nonEmpty(experience.summary) })
    facts.push({ key: `exp-${experience.id}-outcome`, label: `经验 ${n} · 结论`, value: nonEmpty(experience.outcome) })
    facts.push({
      key: `exp-${experience.id}-tags`,
      label: `经验 ${n} · 标签`,
      value: experience.tags.length > 0 ? experience.tags.join('、') : null,
    })
    facts.push({ key: `exp-${experience.id}-created`, label: `经验 ${n} · 时间`, value: fmtTime(experience.created_at) })
  })
  return [{ key: 'memory', title: '经验沉淀', facts }]
}
