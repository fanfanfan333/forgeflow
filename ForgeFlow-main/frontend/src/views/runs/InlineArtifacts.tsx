/**
 * InlineArtifacts — INC36 L1 ⑤「内联 Artifact chip」。
 *
 * 用户：「Artifact 不要单独做成产物面板」——落地方式是**在中列的自然语言结果里
 * 内联产物 chip**（而不是删掉右列 `ArtifactPanel`，右列原样保留以满足既有硬约束）。
 *
 * 每个 chip：产物图标 + 文件名 + 「打开」+「下载」。两个动作都是**真的**：
 *   · 「打开」= 站内预览（把右列产物归档滚入视口），**不调**后端、**不**新开端点；
 *   · 「下载」= 走既有认证通路 `client.ts::downloadArtifact`（JWT/Bearer fetch → Blob →
 *     程序化下载锚点）。失败经 `humanizeError` **如实展示**，绝不假装成功。
 *
 * ⚠️ **testid 纪律**：本组件**严禁**复用既有产物四件套 testid
 * （`artifact-empty` / `artifact-card` / `artifact-preview` / `artifact-download`）——
 * `inc32_workspace.spec.ts` ⑤b 断言它们各自 `count === 1`，复用会把计数变成 2（新红）。
 * 故内联 chip 一律用**全新** testid：`conv-artifact-chip` / `conv-artifact-open` /
 * `conv-artifact-download` / `conv-artifacts`（容器）。
 *
 * 诚实纪律：`artifacts === []` ⇒ 返回 `null`（**内联区不渲染**）——不补占位卡。
 */
import { useState } from 'react'
import { downloadArtifact } from '../../api/client'
import type { RunArtifact } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { IconDocument } from '../../components/icons'

/** 把右列「任务产物」面板滚入视口（站内预览；不存在则静默无操作）。 */
function openArtifactPanel(): void {
  const el = document.querySelector('[data-testid="workspace-col-artifacts"]')
  if (el instanceof HTMLElement) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

export function InlineArtifacts({
  runId,
  artifacts,
}: {
  /** 当前 run id —— 仅供真实下载请求构造。 */
  runId: string
  /** 本次运行的真实产物（`deriveArtifacts`）。空 ⇒ 不渲染。 */
  artifacts: RunArtifact[]
}) {
  if (artifacts.length === 0) return null
  return (
    <section className="conv-artifacts" data-testid="conv-artifacts" aria-label="本次运行产物">
      <p className="conv-artifacts-note">本次运行产出了以下文件：</p>
      {artifacts.map((a) => (
        <InlineArtifact key={a.id} runId={runId} artifact={a} />
      ))}
    </section>
  )
}

function InlineArtifact({ runId, artifact }: { runId: string; artifact: RunArtifact }) {
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
      // 诚实：403 / 404 / 网络错误各有真实码，如实说明，绝不假装成功。
      setError(humanizeError(e, '下载失败').label)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="conv-artifact" data-testid="conv-artifact-chip">
      <span className="conv-artifact-ico" aria-hidden="true">
        <IconDocument width={13} height={13} />
      </span>
      <span className="conv-artifact-name">{filename}</span>
      <button
        type="button"
        className="conv-artifact-btn"
        data-testid="conv-artifact-open"
        onClick={openArtifactPanel}
      >
        打开
      </button>
      <button
        type="button"
        className="conv-artifact-btn"
        data-testid="conv-artifact-download"
        onClick={onDownload}
        disabled={busy}
      >
        {busy ? '下载中…' : '下载'}
      </button>
      {error && (
        <span className="conv-artifact-error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}
