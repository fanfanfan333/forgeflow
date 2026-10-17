import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import '../styles/files.css'

/** Native modal supplies focus trapping, Escape and focus restoration. */
export function PreviewDialog({ children, onClose, className = 'resource-preview-dialog', label = '资源内容预览' }: { children: ReactNode; onClose: () => void; className?: string; label?: string }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const element = dialog.current
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    element?.showModal()
    return () => { element?.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  return createPortal(<dialog ref={dialog} className={className} aria-label={label} onCancel={event => { event.preventDefault(); onClose() }} onClick={event => { if (event.target === event.currentTarget) onClose() }}>
    {children}
  </dialog>, document.body)
}
