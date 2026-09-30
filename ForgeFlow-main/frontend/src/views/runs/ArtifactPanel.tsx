/**
 * ArtifactPanel — the /tasks workspace's right column (INC32 / T04 / T05).
 *
 * Renders the run's REAL deliverables (`GET /runs/{id}`.artifacts, via
 * `realRun.ts::deriveArtifacts`) as a vertical stack of cards. Each card shows
 * the artifact's business title, its format chip, an honest size/rows summary,
 * a **verbatim** preview (the raw body inside a `<pre>` — never reformatted,
 * per P0-2), and a real download action.
 *
 * Honesty rules (data 诚实, §4 of INC32-DESIGN):
 *   · No artifacts  ⇒ the honest empty state「暂无生成结果」(`artifact-empty`),
 *     never a placeholder card (AC-29).
 *   · Card count is **1:1** with `artifacts.length` — never padded, never
 *     invented (AC-27).
 *   · The preview text is **byte-for-byte** `artifacts[i].content`, rendered via
 *     `<pre>` so `##` / other markdown literals survive (AC-28 / AC-30).
 *   · Formats the platform cannot render inline (pdf / xlsx / images / …) are
 *     NOT shown as fake cards — they render an explicit explanation
 *     (`artifact-preview-unsupported`) telling the user to download instead
 *     (ADR-05).
 *   · Size / row count are measured from the real body; when the backend did not
 *     provide a body the value is 「—」 (never a fabricated `0`).
 *   · Download (AC-31 / AC-32) really fetches the file content through an
 *     **authenticated** request (`client.ts::downloadArtifact`) — the endpoint is
 *     behind the JWT/Bearer gate, so a bare `<a download>` would arrive
 *     unauthenticated. A failure is surfaced verbatim, never a silent success.
 *     When there are no artifacts there is simply no download action (not a dead
 *     button).
 *
 * data-testid (only-add discipline, ADR-09): `artifact-panel` / `artifact-card` /
 * `artifact-preview` / `artifact-download` / `artifact-empty` /
 * `artifact-preview-unsupported`.
 */
import { useState } from 'react'
import { downloadArtifact } from '../../api/client'
import type { RunArtifact } from '../../api/client'
import { humanizeError } from '../../api/errors'

/** Formats we can safely show as verbatim text. Everything else is download-only. */
const TEXT_FORMATS = new Set([
  'markdown',
  'md',
  'text',
  'txt',
  'plain',
  'json',
  'yaml',
  'yml',
  'csv',
  'tsv',
  'log',
])

/** Normalise a format string ("md" / ".MD" / " text/plain " → "md" / "text"). */
function normalizeFormat(format: string): string {
  return (format ?? '').trim().toLowerCase().replace(/^\./, '')
}

function isTextFormat(format: string): boolean {
  return TEXT_FORMATS.has(normalizeFormat(format))
}

/** UTF-8 byte length of the body, or `null` when the body is truly absent. */
function artifactBytes(content: string | undefined): number | null {
  if (typeof content !== 'string') return null
  try {
    return new TextEncoder().encode(content).length
  } catch {
    return content.length
  }
}

/** Human-readable byte size, or 「—」 when unmeasured (never a fabricated 0). */
function formatBytes(bytes: number | null): string {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Line count of the body, or 「—」 when unmeasured (never a fabricated 0). */
function formatRows(content: string | undefined): string {
  if (typeof content !== 'string') return '—'
  if (content === '') return '0 行'
  return `${content.split('\n').length} 行`
}

export function ArtifactPanel({
  runId,
  artifacts,
}: {
  /** The selected run's id — used only to build the real download request. */
  runId: string
  /** The run's real deliverables (verbatim). Empty ⇒ honest empty state. */
  artifacts: RunArtifact[]
}) {
  return (
    <section className="panel artifact-panel" data-testid="artifact-panel" aria-label="任务产物">
      <div className="panel-head">
        <div className="title">任务产物</div>
        <div className="actions">
          <span>{artifacts.length} 个产物</span>
        </div>
      </div>
      <div className="panel-body">
        {artifacts.length === 0 ? (
          /* AC-29 — 无产物只渲染诚实空态，**不渲染**空卡片。 */
          <div className="artifact-empty" data-testid="artifact-empty">暂无生成结果</div>
        ) : (
          <div className="artifact-list">
            {artifacts.map((a) => (
              <ArtifactCard key={a.id} runId={runId} artifact={a} />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function ArtifactCard({ runId, artifact }: { runId: string; artifact: RunArtifact }) {
  const bytes = artifactBytes(artifact.content)
  const supported = isTextFormat(artifact.format)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const filename = artifact.title || artifact.kind || artifact.id

  const onDownload = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      // 真实拉取文件内容（带 Authorization 的 fetch → Blob → 存盘）。
      await downloadArtifact(runId, artifact.id, filename)
    } catch (e) {
      // 诚实：下载失败如实说明（403 / 404 / 网络错误各有真实码），绝不假装成功。
      setError(humanizeError(e, '下载失败').label)
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className="artifact-card" data-testid="artifact-card">
      <header className="artifact-head">
        <span className="artifact-title">{artifact.title || artifact.kind || artifact.id}</span>
        <span className="badge mono">{artifact.format || '未知格式'}</span>
      </header>
      <div className="artifact-meta">
        <span className="mono">{formatBytes(bytes)}</span>
        <span className="mono">{formatRows(artifact.content)}</span>
      </div>
      {supported ? (
        /* AC-28 / AC-30 — 逐字原文，`<pre>` 不过 Markdown 渲染（`##` 等字面不丢）。 */
        <pre className="artifact-preview" data-testid="artifact-preview">
          {artifact.content}
        </pre>
      ) : (
        /* ADR-05 — 不支持的类型给诚实说明，**不**渲染假的 PDF / Excel / 图片卡。 */
        <p className="artifact-unsupported" data-testid="artifact-preview-unsupported">
          该产物格式（{artifact.format || '未知'}）暂不支持在线预览，请下载后查看。
        </p>
      )}
      <div className="artifact-actions">
        <button
          type="button"
          className="btn sm"
          data-testid="artifact-download"
          onClick={onDownload}
          disabled={busy}
        >
          {busy ? '下载中…' : '下载'}
        </button>
      </div>
      {error && (
        <p className="artifact-error" role="alert">
          {error}
        </p>
      )}
    </article>
  )
}
