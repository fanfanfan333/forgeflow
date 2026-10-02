/**
 * SkillLibrary — 技能资产中心**左栏**（INC43 S2）。
 *
 * 搜索（`skill-library-search`，驱动 `GET /skills?q=`）+ 范围过滤
 * （`skill-library-scope`，个人/团队/全部）+ 按成熟度分组
 * （`skill-library-group[data-group]`，条目 `skill-library-item`）。
 *
 * 数据全部来自真实 API，本组件只做展示与交互；分组/徽标口径来自 `skillAssets.ts`
 * 的纯函数（禁止在此复制状态判定）。
 *
 * a11y：分组为 `role="listbox"`，条目为 `role="option"`（**刻意不用 `<button>`**——
 * 平铺列表 `SkillTile` 仍是同名按钮，保留唯一按钮语义，避免既有选择器歧义）。
 */

import type { LibraryGroup, SkillScope } from './skillAssets'
import { skillStatusLabel, skillStatusTone } from './skillAssets'

type Props = {
  groups: LibraryGroup[]
  selectedId: string | null
  onSelect: (id: string) => void
  q: string
  onQ: (value: string) => void
  scope: SkillScope
  onScope: (scope: SkillScope) => void
  loading: boolean
  error: string | null
}

const SCOPE_OPTIONS: Array<{ value: SkillScope; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'personal', label: '个人' },
  { value: 'team', label: '团队' },
]

export function SkillLibrary({
  groups,
  selectedId,
  onSelect,
  q,
  onQ,
  scope,
  onScope,
  loading,
  error,
}: Props) {
  const visibleGroups = groups.filter((group) => group.items.length > 0)

  return (
    <aside className="skill-assets-col skill-assets-library" data-testid="skill-library">
      <div className="skill-assets-lib-tools">
        <label className="skill-assets-search">
          <span aria-hidden="true">⌕</span>
          <input
            data-testid="skill-library-search"
            value={q}
            onChange={(e) => onQ(e.target.value)}
            placeholder="搜索技能名称或描述…"
            aria-label="搜索技能"
          />
        </label>
        <select
          data-testid="skill-library-scope"
          className="skill-assets-scope"
          value={scope}
          onChange={(e) => onScope(e.target.value as SkillScope)}
          aria-label="技能范围（个人 / 团队）"
        >
          {SCOPE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {error ? (
        <div className="skill-assets-empty" role="alert">
          加载失败：{error}
        </div>
      ) : loading ? (
        <div className="skill-assets-empty">加载中…</div>
      ) : visibleGroups.length === 0 ? (
        <div className="skill-assets-empty">暂无技能，先从经验编译一个候选吧</div>
      ) : (
        <div className="skill-assets-groups">
          {visibleGroups.map((group) => (
            <section
              key={group.key}
              className="skill-assets-group"
              data-testid="skill-library-group"
              data-group={group.key}
            >
              <div className="skill-assets-group-head">
                <span className="skill-assets-group-label">{group.label}</span>
                <span className="skill-assets-group-count">{group.items.length}</span>
              </div>
              <ul className="skill-assets-group-list" role="listbox" aria-label={group.label}>
                {group.items.map((item) => {
                  const selected = item.id === selectedId
                  const isCandidate = group.key === 'candidate'
                  // 候选：沿用后端 CANDIDATE_STATUSES（不翻译）；技能：走 §3.3 映射。
                  const badgeLabel = isCandidate ? item.status : skillStatusLabel(item.status)
                  const tone = isCandidate ? '' : skillStatusTone(item.status)
                  return (
                    <li
                      key={item.id}
                      role="option"
                      aria-selected={selected}
                      tabIndex={0}
                      data-testid="skill-library-item"
                      data-skill-id={item.id}
                      className={`skill-assets-item${selected ? ' selected' : ''}`}
                      onClick={() => onSelect(item.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onSelect(item.id)
                        }
                      }}
                    >
                      <span className="skill-assets-item-name">{item.name}</span>
                      <span className={`badge ${tone}`}>{badgeLabel}</span>
                    </li>
                  )
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
    </aside>
  )
}
