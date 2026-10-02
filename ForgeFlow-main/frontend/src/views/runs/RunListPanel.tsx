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
import { useDeleteSession, useWorkspaceCreateTask } from '../../api/hooks'
import { useSession } from '../../hooks/useSession'
import { roleConfigFor } from '../../home/roleConfig'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { fmtTime, runStatusMeta } from './realRun'
import { groupRunsByDay } from './conversation'
import { ResourcePicker } from './ResourcePicker'

/**
 * INC36 / T04 —— 左列分组口径由「会话（`session_id`）」改为**按天**（今天 / 昨天 / 更早）。
 *
 * 依据：ChatGPT 式左列先按**时间 proximity** 组织（今天 → 昨天 → 更早），而非会话名。
 * 分组只依据后端真实的 `created_at`（`conversation.groupRunsByDay`，纯函数）：不可解析的
 * 运行归「更早」，绝不臆造。容器 testid（`session-group` / `session-group-title`）与空态
 * testid（`session-history-empty`）**保持不变**（既有 e2e 回归钉子）。
 */

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
  // INC36 / T04 — 按天分组（今天 / 昨天 / 更早）：只依据后端真实 `created_at`。
  const groups = useMemo(() => groupRunsByDay(runs), [runs])
  const busy = create.isPending
  const err = create.error ? humanizeError(create.error, '任务运行失败') : null

  // INC42 / Q6=A —— 删除权限复用既有 `role.canExecute`（只读 viewer 不含
  // `execute:workflows` ⇒ **不渲染**删除按钮，与后端权限对齐；不是禁用死按钮）。
  const session = useSession()
  const canExecute = roleConfigFor(session?.role).canExecute
  // INC42 / Q4=A —— 删除走既有 `useDeleteSession()`（`DELETE /workspace/sessions/{session_id}`，
  // 软删除、保留审计链）。其 `onSuccess` 已失效 `['workspace','sessions']` / `['hub']`
  // ⇒ 本列两源合并（持久会话 + 易失运行）**自动刷新**，无需自造刷新逻辑。
  const remove = useDeleteSession()
  // 待删除会话（点删除按钮先落此态 → 弹二次确认；`null` = 无待办）。
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const deleteError = remove.error ? humanizeError(remove.error, '删除会话失败') : null

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
            任务描述
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
                    // INC42 / Q4=A —— 删除粒度是**整个会话及其全部 run**。左列每行来自
                    // `history.ts::sessionToRunSummary`（持久底：显式带 `session_id`，
                    // 且 `run_id === session_id`）或 `GET /runs` 的易失行 ⇒ 取 `session_id`，
                    // 缺失时回落 `run_id`（与首页 `HomeView.tsx::TaskRow` 逐字同一写法）。
                    const sessionId = r.session_id ?? r.run_id
                    return (
                      <li key={r.run_id}>
                        {/* INC42 —— 删除按钮**不能**嵌进行的 `<button>`（嵌套交互元素非法）⇒
                            行内包一层 `.run-item-row`，行按钮与删除按钮是**同级兄弟**；
                            行按钮的 class / DOM 逐字不变（既有 e2e 选择器不受影响）。 */}
                        <div className="run-item-row">
                          <button
                            type="button"
                            className={`run-item${selected ? ' sel' : ''}`}
                            aria-current={selected ? 'true' : undefined}
                            onClick={() => onSelect(r.run_id)}
                          >
                            <span className="run-item-title">{r.title || r.intent || r.run_id}</span>
                            <span className={`badge ${meta.tone}`.trim()}>{meta.label}</span>
                            <span className="run-item-meta">
                              <span className="mono">{r.run_id.slice(0, 8)}</span>
                              {/* 持久会话映射行不携带步数（step_count 缺席）⇒ 不展示，
                                  绝不渲染伪造的「0 步」（history.ts 诚实纪律）。 */}
                              {typeof r.step_count === 'number' ? ` · ${r.step_count} 步` : ''} ·{' '}
                              {fmtTime(r.created_at)}
                            </span>
                          </button>
                          {/* INC42 / Q6=A —— 仅可执行身份渲染删除按钮（与后端 `execute:workflows` 对齐）。 */}
                          {canExecute && (
                            <button
                              type="button"
                              className="run-item-del"
                              data-testid="tasks-history-delete-btn"
                              title="删除该会话"
                              aria-label="删除该会话"
                              onClick={() => setPendingDelete(sessionId)}
                            >
                              删除
                            </button>
                          )}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}
        {/* INC42 —— 删除失败**必须可见**（不静默）：`role="alert"` + 后端原文（`title`）。 */}
        {deleteError && (
          <p className="af-note warn" role="alert" title={deleteError.detail ?? undefined}>
            {deleteError.label}
          </p>
        )}
      </div>

      {/* INC42 / Q6=A + Q4=A —— 复用既有 `ConfirmDialog` 二次确认；确认后真调
          `DELETE /workspace/sessions/{session_id}`（软删除）。单个 dialog 挂在本面板层
          （不是每行一个），与首页 `HomeView.tsx::RecentTasks` 同一范式。 */}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除该会话？"
        body="删除后该会话及其全部运行将从「历史任务」隐藏。平台不会物理删除记录（保留审计链路）。"
        confirmLabel="删除"
        cancelLabel="取消"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          const target = pendingDelete
          if (!target || remove.isPending) return
          remove.mutate(target, { onSettled: () => setPendingDelete(null) })
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </section>
  )
}
