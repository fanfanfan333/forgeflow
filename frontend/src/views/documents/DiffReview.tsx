/**
 * DiffReview —— INC46 T22「文档 Diff 预览与人工确认」。
 *
 * 把后端 `forgeflow/api/routers/artifact_review.py` 的**真实**能力接到 UI，
 * 消除「后端已挂载但前端不可达」的孤儿能力。消费的端点（逐字段对齐后端契约）：
 *   · `GET  /artifacts/{id}/versions/{v}/diff`    —— 段落 / run / 单元格级 diff；
 *   · `POST /artifacts/{id}/versions/{v}/approve` —— 人工确认 → `committed`；
 *   · `POST /artifacts/{id}/versions/{v}/reject`  —— 拒绝（原因写入 T16）；
 *   · `GET  /artifacts/{id}/versions/{v}/tracked` —— 修订模式 `.tracked.docx`；
 *   · `GET  /artifacts/{id}/versions/{v}/content` —— 该版本制品字节。
 *
 * 诚实纪律（红线）：
 *   · 红线 4：`out_of_region === null` ⇒ 显示「未测量」，**绝不**冒充「无越界」；
 *     `[]` ⇒ 「已测量且无越界」；非空 ⇒ 阻断面板（三种事实三个不同 testid）。
 *   · 红线 3 / 11：`blocked` 时确认按钮默认禁用；只有显式勾选「覆盖越界阻断」
 *     并填理由才可由后端裁决（后端仍可 409，前端原样呈现）。
 *   · 一切计数 / 状态 / `can_approve` 均取后端原值，前端**不派生、不臆造**；
 *     缺失一律「—」（`0` 只在后端真的给 0 时出现）。
 *
 * 三态齐全：`docdiff-loading` / `docdiff-error`(role=alert) / 空态 `docdiff-empty`「—」。
 * data-testid **只增不改不删**（前缀 `docdiff-`，与既有页面零冲突）。
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useParams } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { humanizeError } from '../../api/errors'
import { getSession } from '../../api/client'
import type { ArtifactDiffPreview } from '../../api/client'
import {
  approveArtifactVersion,
  downloadArtifactVersion,
  fetchArtifactDiff,
  rejectArtifactVersion,
} from '../../api/client'
import '../../styles/diff-review.css'

/** 把任意抛出值变成可显示的一句话（后端 detail 原样保留，不吞、不美化）。 */
function errText(error: unknown): string {
  return humanizeError(error).label
}

/** 段落变化 kind → 中文标签（未知 kind 原样输出，不吞）。 */
const PARA_KIND_LABEL: Record<string, string> = {
  modified: '修改',
  added: '新增',
  removed: '删除',
}

/** 计数项的顺序与中文标签（与后端 `DocumentDiff.to_dict()["counts"]` 逐键对应）。 */
const COUNT_KEYS: { key: keyof NonNullable<ArtifactDiffPreview['diff']>['counts']; label: string }[] =
  [
    { key: 'modified', label: '修改段落' },
    { key: 'added', label: '新增段落' },
    { key: 'removed', label: '删除段落' },
    { key: 'numeric_changes', label: '数值变化' },
    { key: 'table_cells', label: '表格单元格' },
  ]

/** 存在 ⇒ 字符串；缺失 / 空 ⇒ 诚实「—」（**绝不**用 0 冒充）。 */
function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/* ------------------------------------------------------------------------- *
 * 展示层（纯函数组件）：给定一份后端预览，逐字渲染。
 * ------------------------------------------------------------------------- */
function DiffCounts({ preview }: { preview: ArtifactDiffPreview }) {
  const counts = preview.diff?.counts
  if (!counts) {
    return (
      <p className="docdiff-empty" data-testid="docdiff-diff-empty">
        —
      </p>
    )
  }
  return (
    <div className="docdiff-counts" data-testid="docdiff-counts">
      {COUNT_KEYS.map(({ key, label }) => (
        <div key={key} className="docdiff-count" data-testid="docdiff-count" data-kind={key}>
          <span className="docdiff-count-value">{text(counts[key])}</span>
          <span className="docdiff-count-label">{label}</span>
        </div>
      ))}
    </div>
  )
}

/** 区间外变化的三种事实：未测量 / 已测量且干净 / 阻断（互斥，绝不合并）。 */
function OutOfRegion({ preview }: { preview: ArtifactDiffPreview }) {
  const oob = preview.out_of_region
  if (oob === null || oob === undefined) {
    return (
      <p className="docdiff-note" data-testid="docdiff-oob-unmeasured">
        区间外变化：未测量
      </p>
    )
  }
  if (oob.length === 0) {
    return (
      <p className="docdiff-note" data-testid="docdiff-oob-clean">
        区间外变化：已测量，无越界
      </p>
    )
  }
  return (
    <div className="docdiff-blocked" data-testid="docdiff-blocked" role="alert">
      <p className="docdiff-blocked-title">
        存在 {oob.length} 处区间外变化 —— 未经人工覆盖不得确认（红线 3）
      </p>
      {oob.map((item, index) => (
        <div
          key={`${item.position}-${index}`}
          className="docdiff-oob-row"
          data-testid="docdiff-oob-row"
          data-position={item.position}
          data-kind={item.kind}
        >
          <span className="docdiff-oob-position">段 {text(item.position)}</span>
          <span className="docdiff-para-kind">{PARA_KIND_LABEL[item.kind] ?? item.kind}</span>
          <span>{text(item.new_text || item.old_text)}</span>
        </div>
      ))}
    </div>
  )
}

function DiffBody({ preview }: { preview: ArtifactDiffPreview }) {
  const diff = preview.diff
  if (!diff) return null
  const paragraphs = diff.paragraphs ?? []
  const tables = diff.tables ?? []

  return (
    <>
      <h2 className="docdiff-section-title">段落变化（{paragraphs.length}）</h2>
      {paragraphs.length === 0 ? (
        <p className="docdiff-empty" data-testid="docdiff-para-empty">
          —
        </p>
      ) : (
        <ul className="docdiff-paras" data-testid="docdiff-paragraphs">
          {paragraphs.map((para, index) => (
            <li
              key={`${para.kind}-${para.old_index ?? 'n'}-${index}`}
              className="docdiff-para"
              data-testid="docdiff-paragraph"
              data-kind={para.kind}
              data-old-index={para.old_index ?? ''}
              data-new-index={para.new_index ?? ''}
            >
              <div className="docdiff-para-head">
                <span className="docdiff-para-kind">{PARA_KIND_LABEL[para.kind] ?? para.kind}</span>
                <span>
                  原段 {text(para.old_index)} → 新段 {text(para.new_index)}
                </span>
              </div>
              <span className="docdiff-para-text docdiff-para-old" data-testid="docdiff-para-old">
                {text(para.old_text)}
              </span>
              <span className="docdiff-para-text" data-testid="docdiff-para-new">
                {text(para.new_text)}
              </span>
              {(para.runs ?? []).length > 0 ? (
                <ul className="docdiff-runs" data-testid="docdiff-runs">
                  {para.runs.map((run) => (
                    <li
                      key={`${run.index}-${run.kind}`}
                      className="docdiff-run"
                      data-testid="docdiff-run"
                      data-index={run.index}
                      data-kind={run.kind}
                    >
                      {text(run.old_text)} → {text(run.new_text)}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      <h2 className="docdiff-section-title">表格变化（{tables.length}）</h2>
      {tables.length === 0 ? (
        <p className="docdiff-empty" data-testid="docdiff-table-empty">
          —
        </p>
      ) : (
        <div className="docdiff-tables">
          {tables.map((table) => (
            <div
              key={table.index}
              className="docdiff-table"
              data-testid="docdiff-table"
              data-index={table.index}
              data-kind={table.kind}
            >
              <span className="docdiff-para-head">
                表 {text(table.index)} · {text(table.old_rows)}×{text(table.old_cols)} →{' '}
                {text(table.new_rows)}×{text(table.new_cols)}
              </span>
              {(table.cells ?? []).map((cell) => (
                <div
                  key={`${cell.row}-${cell.col}`}
                  className="docdiff-cell"
                  data-testid="docdiff-cell"
                  data-row={cell.row}
                  data-col={cell.col}
                  data-kind={cell.kind}
                >
                  <span className="docdiff-oob-position">
                    [{text(cell.row)},{text(cell.col)}]
                  </span>
                  <span>
                    {text(cell.old_text)} → {text(cell.new_text)}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/* ------------------------------------------------------------------------- *
 * 容器：路由参数 → 真实端点 → 三态 + 确认 / 拒绝动作。
 * ------------------------------------------------------------------------- */
export function DiffReviewView() {
  // `strict: false`：本组件在 `/artifacts`（无参）与 `/artifacts/$artifactId/$version`
  // 两条路由下共用，前者 params 为空（与 `LiveRunsView` 的深链播种同款写法）。
  const params = useParams({ strict: false }) as { artifactId?: string; version?: string }
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const artifactId = typeof params.artifactId === 'string' && params.artifactId ? params.artifactId : null
  const versionNumber = Number(params.version)
  const version = Number.isInteger(versionNumber) && versionNumber > 0 ? versionNumber : null
  // 两个参数都齐才发请求（避免为「未选中」发一次注定 404 的请求）。
  const hasTarget = artifactId !== null && version !== null

  const draftIdDefault = ''
  const [draftId, setDraftId] = useState(draftIdDefault)
  const [draftVersion, setDraftVersion] = useState('1')
  const [override, setOverride] = useState(false)
  const [overrideReason, setOverrideReason] = useState('')
  const [rejectReason, setRejectReason] = useState('')
  const [actionLabel, setActionLabel] = useState<string | null>(null)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const queryKey = ['artifact-diff', artifactId, version] as const

  const query = useQuery({
    queryKey,
    queryFn: () => fetchArtifactDiff(artifactId as string, version as number),
    enabled: hasTarget,
  })

  const approve = useMutation({
    mutationFn: () =>
      approveArtifactVersion(artifactId as string, version as number, {
        actor: getSession()?.userId ?? undefined,
        override,
        override_reason: override ? overrideReason || undefined : undefined,
      }),
    onSuccess: (data) => {
      // 动作结果即后端对该动作的响应 ⇒ 直接写回缓存，页面与后端**同一事实**。
      queryClient.setQueryData(queryKey, data)
      setActionLabel(`已确认 · ${data.state}`)
    },
  })

  const reject = useMutation({
    mutationFn: () =>
      rejectArtifactVersion(artifactId as string, version as number, {
        actor: getSession()?.userId ?? undefined,
        reason: rejectReason || undefined,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(queryKey, data)
      setActionLabel(`已拒绝 · ${data.state}`)
    },
  })

  const preview: ArtifactDiffPreview | undefined = query.data
  const isPendingState = preview?.state === 'pending'
  const actionError = approve.error || reject.error ? errText(approve.error ?? reject.error) : null

  let phase: 'loading' | 'error' | 'ready' = 'ready'
  let errorLabel = ''
  if (hasTarget && query.isLoading) {
    phase = 'loading'
  } else if (hasTarget && query.isError) {
    phase = 'error'
    errorLabel = errText(query.error)
  }

  async function runDownload(kind: 'content' | 'tracked') {
    setDownloadError(null)
    try {
      await downloadArtifactVersion(
        artifactId as string,
        version as number,
        kind,
        `${artifactId}.${kind === 'tracked' ? 'tracked' : 'edited'}.docx`,
      )
    } catch (error) {
      // 下载失败**不吞**：如实呈现（红线：不得静默）。
      setDownloadError(errText(error))
    }
  }

  function openTarget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const id = draftId.trim()
    if (!id) return
    const raw = draftVersion.trim() || '1'
    // `to` 走 string 变量（与 `router.tsx::guardedShellChild` 同款写法）：字符串字面量
    // 会被收窄进已注册路由的联合类型，在路由表自注册处触发 TS2322。
    const target: string = `/artifacts/${encodeURIComponent(id)}/${encodeURIComponent(raw)}`
    navigate({ to: target })
  }

  return (
    <div className="docdiff" data-testid="docdiff-page" data-phase={phase}>
      <header className="docdiff-head">
        <div>
          <h1 className="docdiff-title">文档 Diff 预览与确认</h1>
          <p className="docdiff-subtitle">
            段落 / run / 表格单元格三级 diff，人工确认后才产生 committed 版本
          </p>
        </div>
        {hasTarget ? (
          <span className="docdiff-target" data-testid="docdiff-target">
            {artifactId}@v{version}
          </span>
        ) : null}
      </header>

      {/* 无深链 ⇒ 诚实空态 + 可选入口（不伪造任何制品）。 */}
      {!hasTarget ? (
        <>
          <form className="docdiff-picker" data-testid="docdiff-picker" onSubmit={openTarget}>
            <label className="docdiff-field">
              制品 ID
              <input
                className="docdiff-input"
                data-testid="docdiff-artifact-input"
                value={draftId}
                onChange={(event) => setDraftId(event.target.value)}
                placeholder="art_xxx"
              />
            </label>
            <label className="docdiff-field">
              版本
              <input
                className="docdiff-input"
                data-testid="docdiff-version-input"
                value={draftVersion}
                onChange={(event) => setDraftVersion(event.target.value)}
                inputMode="numeric"
              />
            </label>
            <button type="submit" className="btn" data-testid="docdiff-open-btn">
              查看 Diff
            </button>
          </form>
          <p className="docdiff-empty" data-testid="docdiff-empty">
            —
          </p>
        </>
      ) : null}

      {hasTarget ? (
        <section className="docdiff-panel">
          {phase === 'loading' ? (
            <p className="docdiff-loading" data-testid="docdiff-loading">
              Diff 预览加载中…
            </p>
          ) : null}
          {phase === 'error' ? (
            <p className="docdiff-error" data-testid="docdiff-error" role="alert">
              {errorLabel}
            </p>
          ) : null}

          {phase === 'ready' && preview ? (
            <>
              <div className="docdiff-meta" data-testid="docdiff-meta">
                <span className="docdiff-state" data-testid="docdiff-state" data-state={preview.state}>
                  {preview.state}
                </span>
                <span>审批单 {text(preview.approval_id)}</span>
                <span>基线版本 {text(preview.base_version)}</span>
                <span>运行 {text(preview.run_id)}</span>
              </div>

              <DiffCounts preview={preview} />
              <OutOfRegion preview={preview} />
              <DiffBody preview={preview} />

              <div className="docdiff-actions">
                <button
                  type="button"
                  className="btn"
                  data-testid="docdiff-approve-btn"
                  disabled={
                    !isPendingState ||
                    approve.isPending ||
                    (!preview.can_approve && !override)
                  }
                  onClick={() => approve.mutate()}
                >
                  确认并落库
                </button>
                {!preview.can_approve && isPendingState ? (
                  <label className="docdiff-override">
                    <input
                      type="checkbox"
                      data-testid="docdiff-override-check"
                      checked={override}
                      onChange={(event) => setOverride(event.target.checked)}
                    />
                    覆盖越界阻断
                  </label>
                ) : null}
                {override && !preview.can_approve ? (
                  <input
                    className="docdiff-input"
                    data-testid="docdiff-override-reason"
                    value={overrideReason}
                    onChange={(event) => setOverrideReason(event.target.value)}
                    placeholder="覆盖理由（必填）"
                  />
                ) : null}
                <input
                  className="docdiff-input"
                  data-testid="docdiff-reject-reason"
                  value={rejectReason}
                  onChange={(event) => setRejectReason(event.target.value)}
                  placeholder="拒绝原因（可选）"
                />
                <button
                  type="button"
                  className="btn"
                  data-testid="docdiff-reject-btn"
                  disabled={!isPendingState || reject.isPending}
                  onClick={() => reject.mutate()}
                >
                  拒绝
                </button>
                <button
                  type="button"
                  className="btn"
                  data-testid="docdiff-tracked-btn"
                  disabled={!preview.tracked_available}
                  onClick={() => runDownload('tracked')}
                >
                  下载修订模式
                </button>
                <button
                  type="button"
                  className="btn"
                  data-testid="docdiff-content-btn"
                  onClick={() => runDownload('content')}
                >
                  下载制品
                </button>
              </div>

              {actionLabel ? (
                <p
                  className="docdiff-result"
                  data-testid="docdiff-action-result"
                  data-state={preview.state}
                  role="status"
                >
                  {actionLabel}
                </p>
              ) : null}
              {actionError ? (
                <p className="docdiff-action-error" data-testid="docdiff-action-error" role="alert">
                  {actionError}
                </p>
              ) : null}
              {downloadError ? (
                <p className="docdiff-action-error" data-testid="docdiff-download-error" role="alert">
                  下载失败：{downloadError}
                </p>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}
    </div>
  )
}
