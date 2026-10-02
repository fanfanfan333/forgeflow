/**
 * ConfirmDialog — 最小可复用的二次确认弹层（INC40）。
 *
 * 仓库此前**没有**通用确认弹窗；危险操作的先例是自建 overlay（见
 * `frontend/src/components/AuthControls.tsx::SignInDialog`）。本组件沿用同一范式：
 * 自建 overlay + `Escape` 关闭 + 打开时聚焦确认按钮，样式走纯 CSS
 * （`styles/confirm-dialog.css`，双主题 token 驱动）。
 *
 * 破坏性操作（删除资源 / 删除预算 / 归档记忆）一律先经此弹层，避免误触。
 * `data-testid` **只增**：`confirm-dialog` / `confirm-dialog-ok` /
 * `confirm-dialog-cancel`（e2e 以此钉住「点击 → 确认 → 真实请求」）。
 */
import { useEffect, useRef } from 'react'
import '../styles/confirm-dialog.css'

export type ConfirmDialogProps = {
  /** Whether the dialog is shown. When false nothing renders. */
  open: boolean
  /** Dialog heading (Chinese). */
  title: string
  /** Supporting body copy (Chinese). */
  body: string
  /** Confirm-button label; defaults to 「确认」. */
  confirmLabel?: string
  /** Cancel-button label; defaults to 「取消」. */
  cancelLabel?: string
  /** Style the confirm button as a destructive action (`.btn danger`). */
  danger?: boolean
  /** Disable both buttons while the action is in flight. */
  busy?: boolean
  /** Invoked on confirm (the caller fires the real request). */
  onConfirm: () => void
  /** Invoked on cancel / Escape (no request is sent). */
  onCancel: () => void
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const okRef = useRef<HTMLButtonElement | null>(null)

  // Focus the confirm button when the dialog opens (matches SignInDialog).
  useEffect(() => {
    if (open) okRef.current?.focus()
  }, [open])

  // Escape closes (cancels) — unless an action is already in flight.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, onCancel])

  if (!open) return null

  return (
    <div className="confirm-overlay" data-testid="confirm-dialog">
      <div
        className="confirm-card"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <h3 className="confirm-title">{title}</h3>
        <p className="confirm-body">{body}</p>
        <div className="confirm-actions">
          <button
            type="button"
            className="btn ghost sm"
            data-testid="confirm-dialog-cancel"
            disabled={busy}
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            ref={okRef}
            type="button"
            className={`btn sm${danger ? ' danger' : ' primary'}`}
            data-testid="confirm-dialog-ok"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
