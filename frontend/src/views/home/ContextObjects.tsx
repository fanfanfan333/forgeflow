/**
 * ContextObjects — 工作台左栏「上下文 / 对象树」的呈现层（Phase B / T-B02）。
 *
 * 分组（对齐 AUDIT §14.1.2）：
 *   · 会话标题；
 *   · 运行（会话内 runs，每项可选中）；
 *   · 本次涉及 · 文件/数据（`artifacts[]`）、Skill（注入/调用）、记忆（注入）、
 *     变更集（codeplane 代码面）—— 每项可选中 ⇒ `onSelect(object)` 上抛右栏；
 *   · 数据集 Schema/质量 —— 🟥 无端点（G-08）⇒ **显式空态**；
 *   · 资源库（`GET /resources`）—— 单独组件（仅在会话存在时挂载），并**明确标注为
 *     「资源库」而非「本次附件」**（会话级资源读回无端点）。
 *
 * 诚实纪律：无数据一律「暂无数据」/「本次未…」，**绝不臆造 0 或占位数字**。
 * 状态词表唯一源 = `realRun.ts::runStatusMeta`。
 */
import type { ReactNode } from 'react'
import { useResources } from '../../api/hooks'
import { fmtTime, runStatusMeta } from '../runs/realRun'
import type { InspectObject, SessionObjectTree } from './inspect'

type ContextObjectsProps = {
  tree: SessionObjectTree
  selected: InspectObject | null
  onSelect: (object: InspectObject) => void
}

export function ContextObjects({ tree, selected, onSelect }: ContextObjectsProps) {
  const artifacts = tree.objects.filter((o) => o.kind === 'artifact')
  const skills = tree.objects.filter((o) => o.kind === 'skill')
  const memories = tree.objects.filter((o) => o.kind === 'memory')
  const changeSets = tree.objects.filter((o) => o.kind === 'changeset')
  const inputs = tree.objects.filter(o => o.kind === 'resource')
  return (
    <div data-testid="workbench-context-object-tree">
      <div className="workbench-ctx-title">{tree.title}</div>

      <ObjectGroup title="运行" testId="workbench-context-group-runs" empty="还没有任务，去中间发起第一个">
        {tree.runs.map((run) => {
          const meta = runStatusMeta(run.status)
          return (
            <ObjectItem
              key={run.runId}
              testId="workbench-context-run"
              label={run.intent || run.runId}
              meta={fmtTime(run.createdAt)}
              badge={{ tone: meta.tone, label: meta.label }}
              selected={selected?.kind === 'run' && selected.id === run.runId}
              onClick={() => onSelect({ kind: 'run', id: run.runId, label: run.intent || run.runId })}
            />
          )
        })}
      </ObjectGroup>

      <ObjectGroup title="本次涉及 · 文件 / 数据" testId="workbench-context-group-artifacts" empty="本次无产物">
        {artifacts.map((o) => (
          <ObjectItem
            key={`${o.kind}-${o.id}`}
            testId="workbench-context-object"
            label={o.label}
            selected={selected?.kind === o.kind && selected.id === o.id}
            onClick={() => onSelect(o)}
          />
        ))}
      </ObjectGroup>

      <ObjectGroup title="本次涉及 · Skill" testId="workbench-context-group-skills" empty="本次未调用技能">
        {skills.map((o) => (
          <ObjectItem
            key={`${o.kind}-${o.id}`}
            testId="workbench-context-object"
            label={o.label}
            selected={selected?.kind === o.kind && selected.id === o.id}
            onClick={() => onSelect(o)}
          />
        ))}
      </ObjectGroup>

      <ObjectGroup title="本次涉及 · 记忆" testId="workbench-context-group-memory" empty="本次未使用记忆">
        {memories.map((o) => (
          <ObjectItem
            key={`${o.kind}-${o.id}`}
            testId="workbench-context-object"
            label={o.label}
            selected={selected?.kind === o.kind && selected.id === o.id}
            onClick={() => onSelect(o)}
          />
        ))}
      </ObjectGroup>

      <ObjectGroup title="本次涉及 · 变更集" testId="workbench-context-group-changeset" empty="本次无代码变更">
        {changeSets.map((o) => (
          <ObjectItem
            key={`${o.kind}-${o.id}`}
            testId="workbench-context-object"
            label={o.label}
            selected={selected?.kind === o.kind && selected.id === o.id}
            onClick={() => onSelect(o)}
          />
        ))}
      </ObjectGroup>

      <ObjectGroup title="当前输入 · 文件 / 数据" testId="workbench-input-resources" empty="本次未附加文件">
        {inputs.map(o => <ObjectItem key={o.id} testId="workbench-input-resource" label={o.label} selected={selected?.id === o.id} onClick={() => onSelect(o)} />)}
      </ObjectGroup>
      <div className="workbench-ctx-group" data-testid="workbench-context-group-dataset">
        <div className="workbench-ctx-group-title">数据集 Schema / 质量</div>
        <p className="workbench-ctx-note" data-testid="workbench-context-dataset-empty">
          选择当前输入，查看字段、行列数与真实质量检查。
        </p>
      </div>
    </div>
  )
}

/**
 * 会话级资源「读回」当前无端点 —— 这里只列出**租户资源库**（`GET /resources`），
 * 并**明确标注**它不是「本次附件」，避免把资源库冒充成「本次涉及」。
 * 仅在会话存在时由 `ContextPanel` 挂载，避免空首页多发请求。
 */
export function ContextResources() {
  const resourcesQ = useResources({ limit: 20 })
  const items = resourcesQ.data?.items ?? []
  return (
    <div className="workbench-ctx-group" data-testid="workbench-context-group-resources">
      <div className="workbench-ctx-group-title">资源库</div>
      {resourcesQ.isLoading ? (
        <p className="workbench-ctx-note">加载中…</p>
      ) : items.length === 0 ? (
        <p className="workbench-ctx-note" data-testid="workbench-context-resources-empty">
          资源库为空
        </p>
      ) : (
        <ul className="workbench-ctx-list">
          {items.map((r) => (
            <li key={r.id} className="workbench-ctx-resource" data-testid="workbench-context-resource">
              <span className="workbench-ctx-item-name">{r.name || r.id}</span>
              <span className="workbench-ctx-resource-kind">{r.kind}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="workbench-ctx-note">
        会话级附件读回暂无端点：以上为资源库，非本次附件。
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------------- *
 * 内部：分组 + 可选中项（纯呈现）
 * ------------------------------------------------------------------------- */

function ObjectGroup({
  title,
  testId,
  empty,
  children,
}: {
  title: string
  testId: string
  empty: string
  children: ReactNode
}) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : Boolean(children)
  return (
    <div className="workbench-ctx-group" data-testid={testId}>
      <div className="workbench-ctx-group-title">{title}</div>
      {hasChildren ? (
        <ul className="workbench-ctx-list">{children}</ul>
      ) : (
        <p className="workbench-ctx-note">{empty}</p>
      )}
    </div>
  )
}

function ObjectItem({
  testId,
  label,
  meta,
  badge,
  selected,
  onClick,
}: {
  testId: string
  label: string
  meta?: string
  badge?: { tone: string; label: string }
  selected: boolean
  onClick: () => void
}) {
  return (
    <li>
      <button
        type="button"
        className={`workbench-ctx-item${selected ? ' is-selected' : ''}`}
        data-testid={testId}
        aria-current={selected ? 'true' : undefined}
        onClick={onClick}
      >
        <span className="workbench-ctx-item-name">{label}</span>
        {badge && <span className={`badge ${badge.tone}`.trim()}>{badge.label}</span>}
        {meta && <span className="workbench-ctx-item-meta">{meta}</span>}
      </button>
    </li>
  )
}
