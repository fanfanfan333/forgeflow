/**
 * SkillLayerNav —— INC46 T14「图层导航」（L1 元数据 / L2 正文 / L3 资源）。
 *
 * 对齐 T30 渐进披露（`forgeflow/skills/progressive_loader.py` 的 `LAYER_METADATA`
 * `LAYER_BODY` `LAYER_RESOURCE`）：一次只展开一层，索引 → 过程 → 资源。
 * 三层内容**全部**来自选中主体的真实字段（技能：最新版本 `spec`；候选：`draft_spec`），
 * 缺失一律渲染「—」，**绝不补造**字段或假条目（红线 12）。
 *
 * 交互收口：未选中主体 ⇒ 诚实空态 `skill-layer-empty`（「—」）；选中后 tab 可切换，
 * 面板 `skill-layer-panel[data-layer]` 随激活层变化。
 *
 * a11y：`role="tablist"` + `role="tab"`，`aria-selected` 反映激活层。
 */

import { useState } from 'react'

import type { SkillSubject } from './skillAssets'
import '../../styles/skill-flow.css'

export type SkillLayerKey = 'L1' | 'L2' | 'L3'

const LAYERS: Array<{ key: SkillLayerKey; label: string; hint: string }> = [
  { key: 'L1', label: 'L1 元数据', hint: '索引层 · 名称 / 领域 / 状态' },
  { key: 'L2', label: 'L2 正文', hint: '过程层 · 目标 / 步骤' },
  { key: 'L3', label: 'L3 资源', hint: '资源层 · 工具 / 策略 / IO' },
]

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function objectKeys(value: unknown): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.keys(value as Record<string, unknown>)
}

function textOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** 声明步骤：优先 `spec.steps`，其次 `spec.procedure`（都无 ⇒ 空数组）。 */
function declaredSteps(spec: Record<string, unknown>): string[] {
  const steps = stringArray(spec.steps)
  return steps.length > 0 ? steps : stringArray(spec.procedure)
}

/** 一个主体被投影出的三层真实内容（缺字段 ⇒ `null` ⇒「—」）。 */
type LayerFacts = {
  name: string
  domain: string | null
  status: string
  version: string | null
  owner: string | null
  goal: string | null
  steps: string[]
  tools: string[]
  policies: string[]
  ioKeys: string[]
}

function layerFacts(subject: SkillSubject | null): LayerFacts | null {
  if (!subject) return null
  if (subject.kind === 'candidate') {
    const candidate = subject.candidate
    const spec: Record<string, unknown> = candidate.draft_spec ?? {}
    return {
      name: candidate.name,
      domain: textOrNull(candidate.domain),
      status: candidate.status,
      version: null,
      owner: null,
      goal: textOrNull(spec.goal) ?? textOrNull(spec.prompt),
      steps: declaredSteps(spec),
      tools: stringArray(spec.tools),
      policies: stringArray(spec.policies),
      ioKeys: objectKeys(spec.io_schema),
    }
  }
  const skill = subject.skill
  const spec: Record<string, unknown> = subject.version?.spec ?? {}
  return {
    name: skill.name,
    domain: textOrNull(skill.domain),
    status: skill.status,
    version: subject.version?.semver ?? skill.current_version,
    owner: skill.owner,
    goal: textOrNull(spec.goal) ?? textOrNull(spec.prompt),
    steps: declaredSteps(spec),
    tools: stringArray(spec.tools),
    policies: stringArray(spec.policies),
    ioKeys: objectKeys(spec.io_schema),
  }
}

function FactRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="skill-layer-row" data-testid="skill-layer-fact">
      <dt>{label}</dt>
      <dd>{value && value.trim() ? value : '—'}</dd>
    </div>
  )
}

function FactList({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="skill-layer-group">
      <div className="skill-layer-group-title">{label}</div>
      {items.length > 0 ? (
        <ul className="skill-layer-list">
          {items.map((item, i) => (
            <li key={`${item}-${i}`} className="skill-layer-item" data-testid="skill-layer-item">
              {item}
            </li>
          ))}
        </ul>
      ) : (
        <p className="skill-layer-none">—</p>
      )}
    </div>
  )
}

export function SkillLayerNav({ subject }: { subject: SkillSubject | null }) {
  const [active, setActive] = useState<SkillLayerKey>('L1')
  const facts = layerFacts(subject)

  return (
    <div className="skill-layer-nav" data-testid="skill-layer-nav" data-active={active}>
      <div className="skill-layer-tabs" role="tablist" aria-label="技能图层（L1 / L2 / L3）">
        {LAYERS.map((layer) => (
          <button
            key={layer.key}
            type="button"
            role="tab"
            className={`skill-layer-tab${active === layer.key ? ' active' : ''}`}
            data-testid="skill-layer-tab"
            data-layer={layer.key}
            aria-selected={active === layer.key}
            title={layer.hint}
            onClick={() => setActive(layer.key)}
          >
            {layer.label}
          </button>
        ))}
      </div>

      <div
        className="skill-layer-panel"
        data-testid="skill-layer-panel"
        data-layer={active}
        role="tabpanel"
      >
        {!facts ? (
          <p className="skill-layer-empty" data-testid="skill-layer-empty">
            —
          </p>
        ) : active === 'L1' ? (
          <dl className="skill-layer-facts">
            <FactRow label="名称" value={facts.name} />
            <FactRow label="领域" value={facts.domain} />
            <FactRow label="状态" value={facts.status} />
            <FactRow label="版本" value={facts.version ? `v${facts.version}` : null} />
            <FactRow label="Owner" value={facts.owner} />
          </dl>
        ) : active === 'L2' ? (
          <>
            <FactRow label="目标" value={facts.goal} />
            <FactList label="步骤 / 过程" items={facts.steps} />
          </>
        ) : (
          <>
            <FactList label="工具" items={facts.tools} />
            <FactList label="策略" items={facts.policies} />
            <FactList label="IO 字段" items={facts.ioKeys} />
          </>
        )}
      </div>
    </div>
  )
}
