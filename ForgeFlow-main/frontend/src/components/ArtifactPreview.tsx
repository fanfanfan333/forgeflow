import { PreviewDialog } from './PreviewDialog'
import { useEffect, useRef, useState } from 'react'
import { previewArtifact } from '../api/client'
import { humanizeError } from '../api/errors'

export function ArtifactPreview({ runId, artifactId, onClose }: { runId: string; artifactId: string; onClose: () => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof previewArtifact>> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const controller = new AbortController()
    void previewArtifact(runId, artifactId, controller.signal).then(result => { if (!controller.signal.aborted) setData(result) }).catch(err => { if (!controller.signal.aborted) setError(humanizeError(err, '文件预览失败').label) })
    return () => controller.abort()
  }, [runId, artifactId, retry])
  return <PreviewDialog className="artifact-modal-overlay" label="文件内容预览" onClose={onClose}>
    <section className="artifact-modal" aria-label="文件内容预览" data-testid="file-content-preview">
      <header><h2>{data?.title || '文件内容'}</h2><button ref={close} type="button" onClick={onClose} aria-label="关闭预览">×</button></header>
      {!data && !error && <p role="status">正在读取文件内容…</p>}
      {error && <p role="alert">{error} <button onClick={() => { setError(null); setData(null); setRetry(n => n + 1) }}>重试</button></p>}
      {data && <>{data.note && <p className="artifact-preview-note">{data.note}</p>}<pre>{data.text || '文件未包含可提取的文字内容'}</pre>{data.truncated && <p>内容较长，预览已截断，请下载查看完整文件。</p>}</>}
    </section>
  </PreviewDialog>
}
