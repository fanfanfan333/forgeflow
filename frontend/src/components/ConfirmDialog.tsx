import { PreviewDialog } from './PreviewDialog'
/** Native modal keeps keyboard focus inside and restores the invoking button. */
import { useRef } from 'react'
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

  if (!open) return null

  return (
    <PreviewDialog className="confirm-overlay" label={title} onClose={() => { if (!busy) onCancel() }}>
      <div
        className="confirm-card"
        data-testid="confirm-dialog"
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
    </PreviewDialog>
  )
}
