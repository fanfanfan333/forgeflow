/**
 * skillAssets — INC43 S2 技能资产中心（三栏）**纯函数派生层**。
 *
 * 职责（唯一）：把真实 API 数据（`Skill` / `SkillCandidate` / `SkillVersion`）
 * 转成展示态。**不做任何状态判定、不复制既有派生逻辑、不编造数据**：
 *   · 分组 / 计数 / 节点链全部由本文件的纯函数给出，可复算；
 *   · 缺失字段一律 ⇒ `null` ⇒ 渲染「—」，**禁止 `0` 兜底**（§8 诚实纪律）；
 *   · 未知枚举值 ⇒ 中性「未知」，不猜映射。
 *
 * 本文件只 import 类型（`import type`），运行期零依赖，便于单测与静态审阅。
 */

import type { Skill, SkillCandidate, SkillVersion } from '../../api/client'

/* ── 类型（照 §3.1 接口字面量） ─────────────────────────────────────────────── */

export type SkillGroupKey = 'published' | 'testing' | 'candidate' | 'draft'
export type SkillScope = 'all' | 'personal' | 'team'
export type LibraryGroup = { key: SkillGroupKey; label: string; items: Skill[] }
export type ExecNode = { key: string; label: string; declared: boolean }
/** null ⇒ 无该字段 ⇒ 渲染「—」（**禁止** 0 兜底）。 */
export type SkillCounts = { tools: number | null; policies: number | null; tests: number | null }
export type InspectorFacts = {
  /** 存在则读；缺失 ⇒ []。 */
  capabilities: string[]
  /** `spec.tools`（真实存在）；缺失 ⇒ []。 */
  tools: string[]
  /** `SkillVersion.eval_score`；无 ⇒ null（⇒「—」）。 */
  evalScore: number | null
  /** `Skill.usage_count`；无 ⇒ null（⇒「—」）。 */
  usageCount: number | null
}

/**
 * 三栏的“当前主体”：要么是一个已登记技能（+ 其最新版本），要么是一个技能候选。
 * 视图层用它驱动中栏（Engineering）与右栏（Inspector）。纯展示态联合，非后端契约。
 */
export type SkillSubject =
  | { kind: 'skill'; skill: Skill; version: SkillVersion | undefined }
  | { kind: 'candidate'; candidate: SkillCandidate }

/* ── 分组口径 ─────────────────────────────────────────────────────────────── */

/** 左栏分组固定顺序（宽 → 窄的成熟度）。 */
export const SKILL_GROUP_ORDER: SkillGroupKey[] = ['published', 'testing', 'candidate', 'draft']

const SKILL_GROUP_LABELS: Record<SkillGroupKey, string> = {
  published: '已发布',
  testing: '测试中',
  candidate: '候选',
  draft: '草稿',
}

/**
 * `Skill.status` → 左栏分组（design §3.3 / §5 待明确#2）。
 *   · `published` → published；`retired`/`deprecated` → published（无独立分组）；
 *   · `evaluating`/`testing` → testing；`candidate`/`review` → candidate；
 *   · `draft` → draft；**未登记值 → draft 组**（徽标仍显示真实状态，绝不猜映射）。
 */
export function skillGroupKey(status: string): SkillGroupKey {
  switch (status) {
    case 'published':
    case 'retired':
    case 'deprecated':
      return 'published'
    case 'evaluating':
    case 'testing':
      return 'testing'
    case 'candidate':
    case 'review':
      return 'candidate'
    case 'draft':
      return 'draft'
    default:
      return 'draft'
  }
}

/** 卡片 / 条目徽标文案（§3.3）：未登记值 ⇒ 中性「未知」。 */
export function skillStatusLabel(status: string): string {
  switch (status) {
    case 'draft':
      return '草稿'
    case 'evaluating':
    case 'testing':
      return '测试中'
    case 'candidate':
    case 'review':
      return '候选'
    case 'published':
      return '已发布'
    case 'retired':
    case 'deprecated':
      return '已弃用'
    default:
      return '未知'
  }
}

/** 徽标色调：复用 tokens.css 的 `.badge` 变体（'' ⇒ 默认中性）。 */
export function skillStatusTone(status: string): string {
  switch (status) {
    case 'published':
      return 'emerald'
    case 'evaluating':
    case 'testing':
      return 'amber'
    case 'candidate':
    case 'review':
      return 'purple'
    case 'retired':
    case 'deprecated':
      return 'red'
    default:
      return ''
  }
}

/* ── 候选 → Skill 形状适配（仅用于左栏共用 `items: Skill[]`） ───────────────── */

/**
 * 把一个 `SkillCandidate` 适配进 `Skill` 形状，使其能落到左栏的 `items: Skill[]`。
 * 只填候选**真实携带**的字段；其余为结构占位。
 *
 * ⚠️ `usage_count`：候选**没有**使用次数概念，此处置 `0` **仅为满足 `Skill` 类型**，
 * 且**永不渲染**——左栏条目不展示使用次数；右栏对候选走 `candidateInspectorFacts`
 * （使用次数恒为 `null` ⇒「—」）。见 `SkillsView` / `SkillInspector`。
 */
export function candidateAsLibrarySkill(candidate: SkillCandidate): Skill {
  return {
    id: candidate.id,
    tenant_id: candidate.tenant_id,
    name: candidate.name,
    domain: candidate.domain,
    owner: null,
    description: '',
    current_version: null,
    status: candidate.status,
    usage_count: 0,
    featured: false,
    tags: [],
    created_at: candidate.created_at,
    updated_at: candidate.created_at,
  }
}

/**
 * 左栏分组：按 `Skill.status` 归组，并把 `SkillCandidate` 适配后并入 `candidate` 组。
 * 返回**固定四组**（顺序稳定），空组 `items: []`（由视图决定是否渲染）。
 */
export function groupSkillsByStatus(
  skills: Skill[],
  candidates: SkillCandidate[],
): LibraryGroup[] {
  const buckets: Record<SkillGroupKey, Skill[]> = {
    published: [],
    testing: [],
    candidate: [],
    draft: [],
  }
  for (const skill of skills) {
    buckets[skillGroupKey(skill.status)].push(skill)
  }
  for (const candidate of candidates) {
    buckets.candidate.push(candidateAsLibrarySkill(candidate))
  }
  return SKILL_GROUP_ORDER.map((key) => ({
    key,
    label: SKILL_GROUP_LABELS[key],
    items: buckets[key],
  }))
}

/* ── scope 过滤（个人 / 团队） ─────────────────────────────────────────────── */

/**
 * 按真实字段过滤（P1-1）；字段缺失时**诚实降级**，不伪造归属：
 *   · `all`      → 原样返回；
 *   · `personal` → `owner === selfUserId`；**无用户身份 ⇒ 空集**（无法判定，不猜）；
 *   · `team`     → 租户共享（`tenant_id != null`）且非本人所有（与 personal 互斥）。
 */
export function filterByScope(
  skills: Skill[],
  scope: SkillScope,
  selfUserId: string | null,
): Skill[] {
  if (scope === 'all') return skills
  if (scope === 'personal') {
    if (!selfUserId) return []
    return skills.filter((skill) => skill.owner === selfUserId)
  }
  return skills.filter((skill) => skill.tenant_id != null && skill.owner !== selfUserId)
}

/* ── Execution Flow 节点链 ─────────────────────────────────────────────────── */

/** `spec.steps` 缺失时的**前端固定五节点骨架**（declared:false，非已声明契约）。 */
const FLOW_SKELETON: Array<{ key: string; label: string }> = [
  { key: 'inspect', label: 'Inspect' },
  { key: 'plan', label: 'Plan' },
  { key: 'edit', label: 'Edit' },
  { key: 'verify', label: 'Verify' },
  { key: 'artifact', label: 'Artifact' },
]

/** 取「首个可读 token」：去掉行内序号/项目符号后按分隔符切分取首个非空片段。 */
function firstReadableToken(value: unknown): string {
  let text = ''
  if (typeof value === 'string') {
    text = value
  } else if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>
    for (const key of ['tool', 'name', 'title', 'step', 'action']) {
      const v = obj[key]
      if (typeof v === 'string' && v.trim()) {
        text = v
        break
      }
    }
  }
  const cleaned = text.replace(/^\s*(?:\d+[.)、]|[-*•])\s*/, '').trim()
  const token = cleaned.split(/[\s,;:·|/]+/).find((part) => part.length > 0)
  return token ?? cleaned
}

/**
 * Execution Flow 节点链（design §1.3 S2 口径）：
 *   · `spec.steps` 为**非空数组** ⇒ 每步取首个可读 token 作标签，`declared: true`；
 *   · 否则退化为固定五节点骨架，`declared: false`（仅骨架，**不伪造计数**）。
 */
export function execFlowNodes(spec: Record<string, unknown> | undefined): ExecNode[] {
  const steps = spec?.steps
  if (Array.isArray(steps) && steps.length > 0) {
    return steps.map((step, index) => ({
      key: `step-${index}`,
      label: firstReadableToken(step) || `Step ${index + 1}`,
      declared: true,
    }))
  }
  return FLOW_SKELETON.map((node) => ({ key: node.key, label: node.label, declared: false }))
}

/* ── 计数（可复算；缺失 ⇒ null ⇒「—」） ─────────────────────────────────────── */

function arrayLength(value: unknown): number | null {
  return Array.isArray(value) ? value.length : null
}

/**
 * 三计数：`Tools = spec.tools.length`（真实存在）；`Policies`/`Tests` = 对应数组长度。
 * **不存在 ⇒ `null`**（渲染「—」），**禁止 0 兜底**。
 */
export function skillCounts(spec: Record<string, unknown> | undefined): SkillCounts {
  return {
    tools: arrayLength(spec?.tools),
    policies: arrayLength(spec?.policies),
    tests: arrayLength(spec?.tests),
  }
}

/* ── Inspector facts ──────────────────────────────────────────────────────── */

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

/**
 * 右栏事实（design §3.1）：能力/工具来自 `SkillVersion.spec`；评估得分取
 * `SkillVersion.eval_score`、使用次数取 `Skill.usage_count`。**无 ⇒ null**（⇒「—」）。
 */
export function inspectorFacts(skill: Skill, version: SkillVersion | undefined): InspectorFacts {
  const spec = version?.spec
  return {
    capabilities: stringArray(spec?.capabilities),
    tools: stringArray(spec?.tools),
    evalScore: typeof version?.eval_score === 'number' ? version.eval_score : null,
    usageCount: typeof skill.usage_count === 'number' ? skill.usage_count : null,
  }
}

/**
 * 候选主体的右栏事实：候选**没有**版本评估与使用次数，两项测量**恒为 `null`**（⇒「—」）；
 * 能力/工具来自候选真实的 `draft_spec`。**绝不把适配占位值当事实读出。**
 */
export function candidateInspectorFacts(candidate: SkillCandidate): InspectorFacts {
  const spec = candidate.draft_spec
  return {
    capabilities: stringArray(spec?.capabilities),
    tools: stringArray(spec?.tools),
    evalScore: null,
    usageCount: null,
  }
}

/** 中栏 Goal：优先真实 `spec.goal`，其次真实 `spec.prompt`；都没有 ⇒「—」。 */
export function skillGoal(spec: Record<string, unknown> | undefined): string | null {
  if (spec && typeof spec.goal === 'string' && spec.goal.trim()) return spec.goal.trim()
  if (spec && typeof spec.prompt === 'string' && spec.prompt.trim()) return spec.prompt.trim()
  return null
}
