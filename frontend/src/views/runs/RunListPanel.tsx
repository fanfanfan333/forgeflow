/**
 * 运行列表 + 新建任务 — the half of /tasks that was missing.
 *
 * The page used to render only a fixed demo run, so there was nothing to click
 * and no way to start a task. Both now go through the real hub API:
 *   list  → `GET /runs`              (`useHubRuns`)
 *   run   → `POST /workspace/tasks`  (`useWorkspaceCreateTask`) — dispatches
 *                                   asynchronously and returns the handle at
 *                                   once (INC32 / T05). The synchronous
 *                                   `POST /tasks` is unchanged on the backend.
 *
 * Honesty rules: a failed create is surfaced through `humanizeError` (which
 * keeps the status code and the backend's own detail) — never swallowed, and
 * never reported as success.
 */
import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import type { RunSummary } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { useWorkspaceCreateTask } from '../../api/hooks'
import { fmtTime, runStatusMeta } from './realRun'
import { ResourcePicker } from './ResourcePicker'

/**
 * INC32 / T04·T05 —— 左列**会话分组**（additive，纯展示）。把运行按后端真实的
 * `session_id`（`GET /runs` 的 additive 字段）归组；缺 `session_id` 的历史运行
 * 归入单一「历史任务」组。
 *
 * 诚实纪律（AC-42 / AC-43）：分组**只**依据后端真的给了的 `session_id`，绝不在前端
 * 编造会话归属；组标题取该会话**最早一次运行**的意图（`GET /runs` 最新在前，故组内
 * 最后一条即最早），无意图时才回落为「会话 <短号>」/「历史任务」。文案与后端事实一致。
 */
type SessionGroup = { key: string; title: string; runs: RunSummary[] }

function groupRunsBySession(runs: RunSummary[]): SessionGroup[] {
  const groups = new Map<string, SessionGroup>()
  for (const r of runs) {
    const sid = (r.session_id ?? '').trim()
    const key = sid || '__history__'
    const existing = groups.get(key)
    if (existing) {
      existing.runs.push(r)
    } else {
      groups.set(key, { key, title: '', runs: [r] })
    }
  }
  // 标题 = 该会话最早一次运行的意图（组内最后一条）；无意图时回落为业务短号 / 历史任务。
  return Array.from(groups.values()).map((g) => {
    const earliest = g.runs[g.runs.length - 1]
    const intent = (earliest?.intent || earliest?.title || '').trim()
    const title = intent || (g.key === '__history__' ? '历史任务' : `会话 ${g.key.slice(0, 8)}`)
    return { ...g, title }
  })
}

export function RunListPanel({
  runs,
  loading,
  selectedId,
  onSelect,
}: {
  runs: RunSummary[]
  loading: boolean
  selectedId: string | null
  onSelect: (runId: string) => void
}) {
  const create = useWorkspaceCreateTask()
  const [intent, setIntent] = useState('')
  // INC22 W3.1 — 可选的**输入声明**：这两项随任务一起提交到 `context`，让后端
  // 有真实输入可执行（此前只传一句话，`table` 类输入无声明 ⇒ 数据步骤必然受阻）。
  const [table, setTable] = useState('')
  const [paths, setPaths] = useState('')
  // INC25 / T05 —— 已选资源 id（选中项随任务一起声明为 `context.resources`；未选则**不传**键）。
  const [resourceIds, setResourceIds] = useState<string[]>([])
  // INC32 / T04 — 会话分组（additive）：按 session_id 归组，缺省归入「历史任务」。
  const groups = useMemo(() => groupRunsBySession(runs), [runs])
  const busy = create.isPending
  const err = create.error ? humanizeError(create.error, '任务运行失败') : null

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const text = intent.trim()
    if (!text || busy) return
    // 空输入**不传键**（不传空串 / 空数组 —— 那会被后端当成「已声明」而改变判定）。
    const context: Record<string, unknown> = {}
    const tableName = table.trim()
    if (tableName) context.table = tableName
    const pathList = paths
      .split(',')
      .map((p) => p.trim())
      .filter(Boolean)
    if (pathList.length > 0) context.paths = pathList
    // INC25 / T05 —— 选中的资源以 **id 列表**声明（后端 dereference 出真实属性，逐字回填
    // `declared_inputs`）。未选资源时不传 `resources` 键。
    if (resourceIds.length > 0) context.resources = resourceIds
    create.mutate(
      { intent: text, context: Object.keys(context).length > 0 ? context : undefined },
      {
        onSuccess: (handle) => {
          setIntent('')
          setTable('')
          setPaths('')
          setResourceIds([])
          // Select the run we just created so its results are on screen.
          onSelect(handle.run_id)
        },
      },
    )
  }

  return (
    <section className="panel run-list-panel" aria-label="运行列表">
      <div className="panel-head">
        <div className="title">运行列表</div>
        <div className="actions">
          <span>{loading ? '加载中…' : `${runs.length} 次运行`}</span>
        </div>
      </div>

      <div className="panel-body">
        <form className="run-create" onSubmit={submit}>
          <label className="sr-only" htmlFor="run-intent">
            新任务描述
          </label>
          <input
            id="run-intent"
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder="用一句话描述要完成的任务，例如：为 Acme 整理一份销售线索分析摘要"
            disabled={busy}
            autoComplete="off"
          />
          <button type="submit" className="btn primary" disabled={busy || !intent.trim()}>
            {busy ? '运行中…' : '运行任务'}
          </button>
          {/* INC22 W3.1 —— 可选的输入声明（业务表述，无工程术语）。留空则不声明该项。 */}
          <div className="run-declare-row">
            <label className="sr-only" htmlFor="run-declare-table">
              数据表名（可选）
            </label>
            <input
              id="run-declare-table"
              data-testid="run-declare-table"
              value={table}
              onChange={(e) => setTable(e.target.value)}
              placeholder="数据表名（可选）"
              disabled={busy}
              autoComplete="off"
            />
            <label className="sr-only" htmlFor="run-declare-paths">
              文件/仓库路径（可选，逗号分隔）
            </label>
            <input
              id="run-declare-paths"
              data-testid="run-declare-paths"
              value={paths}
              onChange={(e) => setPaths(e.target.value)}
              placeholder="文件/仓库路径（可选，逗号分隔）"
              disabled={busy}
              autoComplete="off"
            />
          </div>
        </form>
        {busy && (
          <p className="af-note" role="status">
            任务正在执行——运行时会把整条链路跑到终态后才返回，请稍候。
          </p>
        )}
        {err && (
          <p className="af-note warn" role="alert" title={err.detail}>
            {err.label}
          </p>
        )}
        {/* INC25 / T05 —— 资源中心：登记五类资源并勾选（选中项随任务声明为 `resources`）。
            既有声明通路（数据表名 / 路径）**保持不变**，本块为纯增量。 */}
        <ResourcePicker
          selectedIds={resourceIds}
          onToggle={(id) =>
            setResourceIds((prev) =>
              prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
            )
          }
          onRegistered={(record) =>
            setResourceIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]))
          }
        />
      </div>

      <div className="panel-body flush">
        {loading ? null : groups.length === 0 ? (
          <>
            {/* 沿用既有「运行列表」空态文案（不变）。 */}
            <p className="af-note">还没有任何运行——用上面的输入框运行第一个任务。</p>
            {/* INC32 / T04 新增「历史」空态：租户无任何历史会话。 */}
            <div className="session-group" data-testid="session-group">
              <div className="session-group-title" data-testid="session-group-title">历史任务</div>
              <p className="af-note" data-testid="session-history-empty">暂无历史任务</p>
            </div>
          </>
        ) : (
          <div className="session-groups">
            {groups.map((g) => (
              <div className="session-group" data-testid="session-group" key={g.key}>
                <div className="session-group-title" data-testid="session-group-title">{g.title}</div>
                <ul className="run-list">
                  {g.runs.map((r) => {
                    const meta = runStatusMeta(r.status)
                    const selected = r.run_id === selectedId
                    return (
                      <li key={r.run_id}>
                        <button
                          type="button"
                          className={`run-item${selected ? ' sel' : ''}`}
                          aria-current={selected ? 'true' : undefined}
                          onClick={() => onSelect(r.run_id)}
                        >
                          <span className="run-item-title">{r.title || r.intent || r.run_id}</span>
                          <span className={`badge ${meta.tone}`.trim()}>{meta.label}</span>
                          <span className="run-item-meta">
                            <span className="mono">{r.run_id.slice(0, 8)}</span> · {r.step_count} 步 ·{' '}
                            {fmtTime(r.created_at)}
                          </span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}
