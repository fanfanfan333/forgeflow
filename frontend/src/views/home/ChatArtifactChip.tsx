/**
 * ChatArtifactChip — Agent 消息内的**产物 chip**（INC43 / T01，P0-8）。
 *
 * 每个 chip：产物图标 + 文件名 + 三个动作（`查看` / `下载` / `继续修改`）。
 *   · `查看`     = 站内「完整视图」深链 `/tasks/{runId}`（产物预览在那里）；
 *   · `下载`     = 既有认证通路 `client.ts::downloadArtifact`（JWT/Bearer fetch →
 *     Blob → 程序化锚点）；其请求 URL 即 `artifactDownloadUrl`（`data-download-url`
 *     暴露同一真实 URL，供断言 / 溯源）。失败经 `humanizeError` **如实展示**；
 *   · `继续修改` = 触发追问（复用 `useChatThread.continueFrom` → `POST /workspace/tasks`
 *     + `parent_run_id`）。
 *
 * testid 纪律：一律用**全新** `chat-artifact-*`，**严禁**复用 `conv-artifact-*` /
 * `artifact-*` 既有四件套（`inc32` 断言各 `count === 1`）。
 */
import { useState } from 'react'
import { artifactDownloadUrl, downloadArtifact } from '../../api/client'
import type { RunArtifact } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { IconDocument } from '../../components/icons'

export function ChatArtifactChip({
  runId,
  artifact,
  onContinue,
}: {
  runId: string
  artifact: RunArtifact
  /** 继续修改：以该 run 为父 run 发起追问。 */
  onContinue: (runId: string, intent: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const filename = artifact.title || artifact.kind || artifact.id

  const onDownload = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await downloadArtifact(runId, artifact.id, filename)
    } catch (e) {
      // 403 / 404 / 网络错误各有真实码——如实说明，绝不假装成功。
      setError(humanizeError(e, '下载失败').label)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="chat-artifact-chip" data-testid="chat-artifact-chip">
      <span className="chat-artifact-ico" aria-hidden="true">
        <IconDocument width={14} height={14} />
      </span>
      <span className="chat-artifact-name">{filename}</span>
      <div className="chat-artifact-actions">
        <a
          className="chat-artifact-btn"
          data-testid="chat-artifact-view"
          href={`/tasks/${runId}`}
          title="在完整视图中查看该产物"
        >
          查看
        </a>
        <button
          type="button"
          className="chat-artifact-btn"
          data-testid="chat-artifact-download"
          data-download-url={artifactDownloadUrl(runId, artifact.id)}
          onClick={onDownload}
          disabled={busy}
        >
          {busy ? '下载中…' : '下载'}
        </button>
        <button
          type="button"
          className="chat-artifact-btn"
          data-testid="chat-artifact-continue"
          onClick={() => onContinue(runId, `继续修改「${filename}」`)}
        >
          继续修改
        </button>
      </div>
      {error && (
        <span className="chat-artifact-error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}
