/**
 * INC25 / T05 / W1 —— 资源中心：五类资源（文件 / 数据表 / 代码仓库 / 知识库 / 接口）
 * 的**登记**与**选择**，挂在「新建任务」面板上。
 *
 * INC26 / T02 —— 上传体验升级（P0-1/P0-2/P0-3/P0-4）：
 *   * **拖拽区 + 多选**（`<input type="file" multiple>`）：拖入或选中 N 个文件 ⇒
 *     N 行独立上传项，每行有自己的状态（待上传 / 上传中 / 已解析 / 失败+逐字原因）。
 *     逐个 `await` **串行**上传；**一个失败不连坐其余**。
 *   * **上传前预检**：用 T01 的 `GET /resources/limits` 结果校验类型与大小，被拦的行
 *     原地显示逐字原因，**不发任何请求**。上限/白名单**绝不写死**（后端单一事实源）。
 *   * **预览**：接既有 `GET /resources/{id}/preview`；表格渲染表头+行，文本渲染前 N 行，
 *     `truncated` 显示截断提示；非文件类呈现后端诚实空态（`available=false`+note）。
 *   * **`kind` 筛选 + 历史复用**：类型筛选走 `hubApi.resources({ kind })`；历史资源可
 *     直接勾选复用（与本次新登记的资源语义一致）。
 *
 * 诚实纪律：
 *   * 摘要只呈现后端**真实统计**（行/列/字符/页数/文件数/语言）；未测量即不显示那一位，
 *     **绝不**补 0；无真实摘要时给诚实空文案。
 *   * 登记/上传失败逐字呈现后端原因（413 超限 / 400 类型不支持 / …），**绝不**吞错、
 *     **绝不**假装成功。
 *   * 空态文案固定为「未添加资源时，Agent 不会读取任何文件、仓库或数据库」。
 *
 * `data-testid`：既有项（`resource-add` / `resource-kind-*` / `resource-list` /
 * `resource-card` / `resource-empty-note`）**保留且行为不变**；本次**只增**：
 * `resource-dropzone` / `resource-upload-row` / `resource-upload-status` /
 * `resource-preview` / `resource-preview-truncated` / `resource-kind-filter` /
 * `resource-limits-note`（AC-19）。
 */
import { useState } from 'react'
import type { RegisterResourceInput, ResourceRecord } from '../../api/client'
import { humanizeError } from '../../api/errors'
import {
  useRegisterResource,
  useResourceLimits,
  useResourcePreview,
  useResources,
} from '../../api/hooks'

/** 五类资源的入口（顺序稳定；每类一个可寻址 `data-testid`，AC-1）。 */
const KIND_META: { kind: string; testid: string; label: string }[] = [
  { kind: 'file', testid: 'resource-kind-file', label: '文件' },
  { kind: 'database', testid: 'resource-kind-database', label: '数据表' },
  { kind: 'git_repo', testid: 'resource-kind-git_repo', label: '代码仓库' },
  { kind: 'knowledge_base', testid: 'resource-kind-knowledge_base', label: '知识库' },
  { kind: 'api', testid: 'resource-kind-api', label: '接口（API）' },
]

/** 代码来源类型（与后端 `resources/code_sources.py::CODE_SOURCE_TYPES` 同源）。 */
const CODE_SOURCE_TYPES: { value: string; label: string }[] = [
  { value: 'local_path', label: '本地路径' },
  { value: 'github', label: 'GitHub 仓库' },
  { value: 'gitlab', label: 'GitLab 仓库' },
  { value: 'zip', label: 'ZIP 压缩包' },
]

/** 预览默认取前 N 行。 */
const PREVIEW_ROWS = 20

function kindLabel(kind: string): string {
  return KIND_META.find((k) => k.kind === kind)?.label ?? kind
}

/** 小写扩展名（含点）；无扩展名返回空串。 */
function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

function statusLabel(status: string): string {
  switch ((status ?? '').toLowerCase()) {
    case 'parsed':
      return '已解析'
    case 'metadata_only':
      return '仅元数据'
    case 'ignored':
      return '已忽略'
    case 'registered':
      return '已登记'
    case 'unavailable':
      return '不可用'
    default:
      return status || '已登记'
  }
}

function statusTone(status: string): string {
  switch ((status ?? '').toLowerCase()) {
    case 'parsed':
      return 'emerald'
    case 'metadata_only':
    case 'ignored':
      return 'amber'
    case 'unavailable':
      return 'red'
    default:
      return 'blue'
  }
}

/** 摘要行：只拼后端真实存在的数字/字段；一个都没有时给诚实空文案。 */
function summaryLine(r: ResourceRecord): string {
  const s = r.summary ?? {}
  const loc = r.locator ?? {}
  const bits: string[] = []
  if (typeof s.rows === 'number') bits.push(`${s.rows} 行`)
  if (typeof s.columns === 'number') bits.push(`${s.columns} 列`)
  if (typeof s.chars === 'number') bits.push(`${s.chars} 字符`)
  if (typeof s.pages === 'number') bits.push(`${s.pages} 页`)
  if (Array.isArray(s.fields) && s.fields.length > 0) {
    bits.push(`字段：${s.fields.slice(0, 6).join('、')}`)
  }
  if (typeof loc.files_count === 'number') bits.push(`${loc.files_count} 个文件`)
  if (Array.isArray(loc.languages) && loc.languages.length > 0) {
    bits.push(`语言：${loc.languages.slice(0, 4).join('、')}`)
  }
  if (Array.isArray(s.keywords) && s.keywords.length > 0) {
    bits.push(`关键词：${s.keywords.slice(0, 5).join('、')}`)
  }
  if (bits.length > 0) return bits.join(' · ')
  return s.note || '（无可展示的摘要）'
}

/** 一个上传项：本地行状态 + 后端状态的诚实映射。 */
type UploadItem = {
  id: string
  file: File
  name: string
  size: number
  /** 行级状态：待上传 / 上传中 / 完成（含后端真实 status）/ 失败（含逐字原因）。 */
  state: 'pending' | 'uploading' | 'done' | 'failed'
  /** 完成后端返回的真实 status（parsed / metadata_only / ignored …）。 */
  backendStatus?: string
  resourceId?: string
  /** 失败原因（预检拦截或后端逐字原因）。 */
  reason?: string
}

export function ResourcePicker({
  selectedIds,
  onToggle,
  onRegistered,
}: {
  selectedIds: string[]
  onToggle: (id: string) => void
  onRegistered: (record: ResourceRecord) => void
}) {
  const [filterKind, setFilterKind] = useState<string | null>(null)
  const list = useResources({ kind: filterKind ?? undefined, limit: 50 })
  const limits = useResourceLimits()
  const register = useRegisterResource()
  const [open, setOpen] = useState(false)
  const [activeKind, setActiveKind] = useState<string | null>(null)
  // 非文件类资源用两个文本槽（语义随类型变化）。
  const [text1, setText1] = useState('')
  const [text2, setText2] = useState('')
  const [sourceType, setSourceType] = useState('local_path')
  // 上传行（逐文件独立成败）+ 拖拽高亮 + 预览目标。
  const [uploads, setUploads] = useState<UploadItem[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [previewId, setPreviewId] = useState<string | null>(null)

  const busy = register.isPending
  const err = register.error ? humanizeError(register.error, '资源登记失败') : null
  const resources = list.data?.items ?? []
  const limitsData = limits.data

  const resetForm = () => {
    setText1('')
    setText2('')
    setActiveKind(null)
  }

  /** 上传前预检：类型 + 大小。被拦 ⇒ 返回逐字原因（**不发请求**）；放行 ⇒ null。 */
  const precheck = (file: File): string | null => {
    if (!limitsData) return null // 上限尚未取回 ⇒ 交由后端把关（仍不写死）
    const ext = extOf(file.name)
    const exts = limitsData.supported_extensions ?? []
    if (exts.length > 0 && !exts.includes(ext)) {
      return `不支持的文件类型：${ext || '(无扩展名)'}；支持：${exts.join('、')}`
    }
    if (typeof limitsData.max_bytes === 'number' && file.size > limitsData.max_bytes) {
      return `文件为 ${file.size} 字节，超过单文件上限 ${limitsData.max_bytes} 字节`
    }
    return null
  }

  const setRow = (id: string, patch: Partial<UploadItem>) => {
    setUploads((prev) => prev.map((u) => (u.id === id ? { ...u, ...patch } : u)))
  }

  /** 串行上传：逐个 `await`，一个失败不连坐其余（Q2）。 */
  const runUploads = async (queue: UploadItem[]) => {
    for (const item of queue) {
      setRow(item.id, { state: 'uploading' })
      try {
        const record = await register.mutateAsync({ kind: 'file', file: item.file })
        setRow(item.id, {
          state: 'done',
          backendStatus: record.status,
          resourceId: record.id,
        })
        onRegistered(record)
      } catch (e) {
        const h = humanizeError(e, '上传失败')
        setRow(item.id, { state: 'failed', reason: h.label })
      }
    }
  }

  /** 收下一批文件：逐文件预检 ⇒ 被拦的行原地失败（不发请求），其余串行上传。 */
  const acceptFiles = (input: FileList | File[] | null | undefined) => {
    const files = input ? Array.from(input) : []
    if (files.length === 0) return
    const stamp = Date.now()
    const items: UploadItem[] = files.map((file, i) => {
      const reason = precheck(file)
      return {
        id: `${stamp}-${i}-${file.name}`,
        file,
        name: file.name,
        size: file.size,
        state: reason ? 'failed' : 'pending',
        reason: reason ?? undefined,
      }
    })
    setUploads((prev) => [...prev, ...items])
    const queued = items.filter((it) => it.state === 'pending')
    if (queued.length > 0) void runUploads(queued)
  }

  // ---- 非文件类登记（既有路径，保持行为不变） --------------------------------
  const buildInput = (): RegisterResourceInput | null => {
    const a = text1.trim()
    const b = text2.trim()
    switch (activeKind) {
      case 'database':
        return a ? { kind: 'database', table: a } : null
      case 'git_repo':
        return a ? { kind: 'git_repo', source_type: sourceType, identifier: a, branch: b } : null
      case 'knowledge_base':
        return a ? { kind: 'knowledge_base', kb_id: a, scope: b } : null
      case 'api':
        return a ? { kind: 'api', connector: a, base_url: b } : null
      default:
        return null
    }
  }

  const submit = () => {
    const input = buildInput()
    if (!input || busy) return
    register.mutate(input, {
      onSuccess: (record) => {
        resetForm()
        onRegistered(record)
      },
    })
  }

  const preview = useResourcePreview(previewId, PREVIEW_ROWS)
  const previewName = resources.find((r) => r.id === previewId)?.name ?? previewId ?? ''

  return (
    <div className="resource-picker">
      <div className="resource-picker-head">
        <button
          type="button"
          className="btn ghost sm"
          data-testid="resource-add"
          aria-expanded={open}
          aria-controls="resource-kind-row"
          onClick={() => setOpen((v) => !v)}
        >
          + 添加资源
        </button>
        <span className="resource-picker-hint">
          未添加资源时，Agent 不会读取任何文件、仓库或数据库
        </span>
      </div>

      {/* 五类资源入口：**仅**在展开后出现；各自独立可寻址（AC-1）。 */}
      {open && (
        <div id="resource-kind-row" className="resource-kind-row" role="group" aria-label="资源类型">
          {KIND_META.map((k) => (
            <button
              key={k.kind}
              type="button"
              className={`resource-kind-btn${activeKind === k.kind ? ' on' : ''}`}
              data-testid={k.testid}
              onClick={() => {
                resetForm()
                setActiveKind(k.kind)
              }}
            >
              {k.label}
            </button>
          ))}
        </div>
      )}

      {/* 登记表单（按类型不同；提交即真调 `/resources*`）。 */}
      {open && activeKind && (
        <div className="resource-form" role="group" aria-label="资源登记">
          {activeKind === 'file' ? (
            <div className="resource-file-block">
              <label
                className={`resource-dropzone${dragOver ? ' over' : ''}`}
                data-testid="resource-dropzone"
                onDragOver={(e) => {
                  e.preventDefault()
                  setDragOver(true)
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault()
                  setDragOver(false)
                  acceptFiles(e.dataTransfer?.files)
                }}
              >
                <input
                  type="file"
                  multiple
                  className="resource-dropzone-input"
                  disabled={busy}
                  onChange={(e) => {
                    acceptFiles(e.target.files)
                    e.target.value = ''
                  }}
                />
                <span className="resource-dropzone-hint">
                  拖拽文件到此处，或点击选择（可多选；逐个独立上传）
                </span>
              </label>
              {limitsData && (
                <p className="resource-limits-note" data-testid="resource-limits-note">
                  单文件上限 {limitsData.max_bytes.toLocaleString()} 字节 · 支持类型：
                  {(limitsData.supported_extensions ?? []).join('、')}
                </p>
              )}

              {uploads.length > 0 && (
                <ul className="resource-upload-list">
                  {uploads.map((u) => (
                    <li className="resource-upload-row" data-testid="resource-upload-row" key={u.id}>
                      <span className="resource-upload-name" title={u.name}>
                        {u.name}
                      </span>
                      <span
                        className={`resource-upload-status ${u.state}`}
                        data-testid="resource-upload-status"
                      >
                        {u.state === 'pending' && '待上传'}
                        {u.state === 'uploading' && '上传中…'}
                        {u.state === 'done' && statusLabel(u.backendStatus ?? 'parsed')}
                        {u.state === 'failed' && `失败：${u.reason ?? '未知原因'}`}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <>
              {activeKind === 'git_repo' && (
                <label className="resource-form-field">
                  <span className="resource-form-label">来源类型</span>
                  <select
                    value={sourceType}
                    disabled={busy}
                    onChange={(e) => setSourceType(e.target.value)}
                  >
                    {CODE_SOURCE_TYPES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="resource-form-field">
                <span className="resource-form-label">{FIELD_LABELS[activeKind].first}</span>
                <input
                  value={text1}
                  disabled={busy}
                  autoComplete="off"
                  onChange={(e) => setText1(e.target.value)}
                />
              </label>
              {FIELD_LABELS[activeKind].second && (
                <label className="resource-form-field">
                  <span className="resource-form-label">{FIELD_LABELS[activeKind].second}</span>
                  <input
                    value={text2}
                    disabled={busy}
                    autoComplete="off"
                    onChange={(e) => setText2(e.target.value)}
                  />
                </label>
              )}
              <div className="resource-form-actions">
                <button type="button" className="btn primary sm" disabled={busy} onClick={submit}>
                  {busy ? '登记中…' : '登记'}
                </button>
                <button type="button" className="btn ghost sm" disabled={busy} onClick={resetForm}>
                  取消
                </button>
              </div>
            </>
          )}
          {err && (
            <p className="af-note warn" role="alert" title={err.detail}>
              {err.label}
            </p>
          )}
        </div>
      )}

      {/* 类型筛选：驱动 `GET /resources?kind=`（历史复用入口）。 */}
      <label className="resource-filter-row">
        <span className="resource-form-label">按类型筛选</span>
        <select
          className="resource-kind-filter"
          data-testid="resource-kind-filter"
          value={filterKind ?? ''}
          onChange={(e) => setFilterKind(e.target.value || null)}
        >
          <option value="">全部类型</option>
          {KIND_META.map((k) => (
            <option key={k.kind} value={k.kind}>
              {k.label}
            </option>
          ))}
        </select>
      </label>

      {/* 资源清单（真实登记项；可选，选中项随任务一起声明）。 */}
      {list.isError ? (
        <p className="af-note warn" role="alert">
          {humanizeError(list.error, '资源列表加载失败').label}
        </p>
      ) : resources.length > 0 ? (
        <ul className="resource-list" data-testid="resource-list">
          {resources.map((r) => (
            <li className="resource-card" data-testid="resource-card" key={r.id}>
              <div className="resource-card-row">
                <label className="resource-card-pick">
                  <input
                    type="checkbox"
                    checked={selectedIds.includes(r.id)}
                    onChange={() => onToggle(r.id)}
                  />
                  <span className="resource-card-main">
                    <span className="resource-card-top">
                      <span className="resource-kind-tag">{kindLabel(r.kind)}</span>
                      <span className="resource-name">{r.name || r.id}</span>
                      <span className={`badge ${statusTone(r.status)}`.trim()}>
                        {statusLabel(r.status)}
                      </span>
                    </span>
                    <span className="resource-summary">{summaryLine(r)}</span>
                    {r.summary?.stub && (
                      <span className="resource-stub">离线档：摘要为占位标注，不含真实业务数据</span>
                    )}
                    {r.detail && <span className="resource-detail">{r.detail}</span>}
                  </span>
                </label>
                <button
                  type="button"
                  className="btn ghost sm resource-preview-toggle"
                  onClick={() => setPreviewId((cur) => (cur === r.id ? null : r.id))}
                >
                  {previewId === r.id ? '收起预览' : '预览'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="resource-empty-note res-subtle" data-testid="resource-empty-note">
          未添加资源时，Agent 不会读取任何文件、仓库或数据库
        </p>
      )}

      {/* 内容预览（真实调用 `GET /resources/{id}/preview`）。 */}
      {previewId && (
        <div className="resource-preview" data-testid="resource-preview">
          <div className="resource-preview-head">
            <span className="resource-preview-title">预览：{previewName}</span>
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => setPreviewId(null)}
            >
              关闭
            </button>
          </div>
          {preview.isLoading && <p className="resource-preview-note">加载中…</p>}
          {preview.isError && (
            <p className="af-note warn" role="alert">
              {humanizeError(preview.error, '预览加载失败').label}
            </p>
          )}
          {preview.data && !preview.data.available && (
            <p className="resource-preview-note">
              {preview.data.note || '该资源暂不支持内容预览'}
            </p>
          )}
          {preview.data && preview.data.available && preview.data.format === 'table' && (
            <div className="resource-preview-tablewrap">
              <table className="resource-preview-table">
                <thead>
                  <tr>
                    {preview.data.columns.map((c, i) => (
                      <th key={`${c}-${i}`}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, ri) => (
                    <tr key={ri}>
                      {row.map((cell, ci) => (
                        <td key={ci}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {preview.data && preview.data.available && preview.data.format === 'text' && (
            <pre className="resource-preview-text">{preview.data.content}</pre>
          )}
          {preview.data && preview.data.truncated && (
            <p
              className="af-note warn resource-preview-truncated"
              data-testid="resource-preview-truncated"
            >
              已截断：{preview.data.note || `仅预览前 ${PREVIEW_ROWS} 行`}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

/** 各类非文件资源的两个文本槽标签（第二项可为 `null` ⇒ 不渲染）。 */
const FIELD_LABELS: Record<string, { first: string; second: string | null }> = {
  database: { first: '数据表名', second: null },
  git_repo: { first: '仓库 / 路径 / 文件名', second: '分支（可选）' },
  knowledge_base: { first: '知识库标识', second: '范围 / 命名空间（可选）' },
  api: { first: '连接器标识', second: 'Base URL（可选）' },
}
