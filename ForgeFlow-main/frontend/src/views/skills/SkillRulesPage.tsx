/**
 * SkillRulesPage —— 技能资产中心「规则」页（INC46 T06）。
 *
 * 只读消费 `GET /skills/{id}/rules`：租户抽取出的 `must` / `must_not` 规则，
 * 每条带可复算的 `support` / `confidence`，以及**是否被平台真正强制**
 * （`enforced` + `enforcement` 依据）。下方给出该技能声明工具的强制摘要
 * （`tool_permissions` 单一真源分类）。
 *
 * 诚实纪律（红线 4 / §8）：
 *   · 未选中技能 ⇒ 诚实空态（`skill-rules-empty`，文案「选择一个技能」）；
 *   · 无规则 ⇒ 真实空态（「未发现规则」），**不伪造** 0 条以外的任何数字；
 *   · 加载中 / 失败都有诚实态，绝不渲染假的规则或假的强制状态。
 *
 * 零新端点：全部数据来自 `useSkillRules`（真实后端）。
 */

import { humanizeError } from '../../api/errors'
import { useSkillRules } from '../../api/hooks'
import type { SkillRuleItem } from '../../api/client'

/** 强制徽标：真被强制 ⇒ 实心；仅建议 ⇒ 中性（**不夸大**）。 */
function EnforcedBadge({ rule }: { rule: SkillRuleItem }) {
  return (
    <span
      className={`skill-rules-enforced ${rule.enforced ? 'is-enforced' : 'is-advisory'}`}
      title={rule.enforcement}
      data-enforced={rule.enforced ? 'true' : 'false'}
    >
      {rule.enforced ? '已强制' : '仅建议'}
    </span>
  )
}

/** 规则文本 + 证据（support / confidence / 来源 run + 强制依据）。 */
function RuleRow({ rule }: { rule: SkillRuleItem }) {
  return (
    <li className="skill-rules-item" data-testid="skill-rules-item">
      <div className="skill-rules-item-head">
        <span className="skill-rules-text">{rule.rule_text}</span>
        <EnforcedBadge rule={rule} />
      </div>
      <div className="skill-rules-item-meta text-mono">
        <span>support {rule.support}</span>
        <span>confidence {rule.confidence.toFixed(4)}</span>
        <span>来源 run {rule.source_run_ids.length}</span>
      </div>
      <p className="skill-rules-evidence">{rule.enforcement}</p>
    </li>
  )
}

function RuleGroup({
  title,
  rules,
  testId,
}: {
  title: string
  rules: SkillRuleItem[]
  testId: string
}) {
  // INC46 T14 守卫：桩 / 降级响应可能整体缺 `rules`（或给错形状）。
  // 缺失 ⇒ 计数与正文一律「—」；**真实空数组 `[]` 仍按 0 计数**（既有语义不变）。
  const hasRules = Array.isArray(rules)
  return (
    <div className="skill-rules-group">
      <div className="skill-rules-group-title">
        {title} <span className="skill-rules-group-count text-mono">{hasRules ? rules.length : '—'}</span>
      </div>
      {!hasRules ? (
        <p className="skill-rules-none">—</p>
      ) : rules.length > 0 ? (
        <ul className="skill-rules-list" data-testid={testId}>
          {rules.map((rule) => (
            <RuleRow key={rule.rule_id} rule={rule} />
          ))}
        </ul>
      ) : (
        <p className="skill-rules-none">未发现该类规则</p>
      )}
    </div>
  )
}

export function SkillRulesPage({ skillId }: { skillId: string | null }) {
  const rulesQ = useSkillRules(skillId)

  if (!skillId) {
    return (
      <section className="skill-insights-block" data-testid="skill-rules">
        <div className="skill-insights-block-title">规则</div>
        <p className="skill-insights-empty" data-testid="skill-rules-empty">
          选择一个技能，查看它抽取出的 must / must_not 规则
        </p>
      </section>
    )
  }

  if (rulesQ.isLoading) {
    return (
      <section className="skill-insights-block" data-testid="skill-rules">
        <div className="skill-insights-block-title">规则</div>
        <p className="skill-insights-loading">规则加载中…</p>
      </section>
    )
  }

  if (rulesQ.isError) {
    return (
      <section className="skill-insights-block" data-testid="skill-rules">
        <div className="skill-insights-block-title">规则</div>
        <p className="skill-insights-error" role="alert">
          {humanizeError(rulesQ.error, '规则加载失败').label}
        </p>
      </section>
    )
  }

  const data = rulesQ.data
  if (!data) {
    return (
      <section className="skill-insights-block" data-testid="skill-rules">
        <div className="skill-insights-block-title">规则</div>
        <p className="skill-insights-empty">暂无规则数据</p>
      </section>
    )
  }

  const enforcement = data.enforcement
  // INC46 T14 守卫：`enforcement` 可能整体缺失，或 `declared_tools` / `blocked_tools`
  // 形状不对。缺失 ⇒「—」；**真实数组 `[]` 仍按 0 计数**（既有语义不变）。
  const blockedTools = Array.isArray(enforcement?.blocked_tools) ? enforcement.blocked_tools : null
  const declaredToolCount = Array.isArray(enforcement?.declared_tools)
    ? enforcement.declared_tools.length
    : '—'
  return (
    <section className="skill-insights-block" data-testid="skill-rules">
      <div className="skill-insights-block-title">规则</div>

      <RuleGroup title="必须（must）" rules={data.must} testId="skill-rules-must" />
      <RuleGroup title="禁止（must not）" rules={data.must_not} testId="skill-rules-not" />

      <div className="skill-rules-enforcement" data-testid="skill-rules-enforcement">
        <div className="skill-rules-group-title">
          强制摘要 <span className="text-mono">（{enforcement?.source ?? '—'}）</span>
        </div>
        <div className="skill-rules-enf-row text-mono">
          <span>声明工具 {declaredToolCount}</span>
          <span>风险等级 {enforcement?.risk_level || '—'}</span>
          <span>被阻断 {blockedTools ? blockedTools.length : '—'}</span>
        </div>
        {blockedTools && blockedTools.length > 0 ? (
          <ul className="skill-rules-tools text-mono">
            {blockedTools.map((tool) => (
              <li key={tool} className="skill-rules-tool-blocked">
                {tool} (DANGEROUS)
              </li>
            ))}
          </ul>
        ) : blockedTools ? (
          <p className="skill-rules-none">该技能未声明任何被平台阻断的 DANGEROUS 工具</p>
        ) : (
          <p className="skill-rules-none">—</p>
        )}
      </div>
    </section>
  )
}
