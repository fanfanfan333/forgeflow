import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, login, logout } from '../api/client'
import { useSession } from '../hooks/useSession'
import { OPEN_SIGNIN_EVENT, openSignIn } from './authEvents'
import '../styles/auth.css'

function initials(userId: string): string {
  const parts = userId.split(/[\s_.-]/).filter(Boolean)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || userId.slice(0, 2).toUpperCase()
}

function SignInDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [userId, setUserId] = useState('manager-1')
  const [password, setPassword] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const [needsMfa, setNeedsMfa] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    firstFieldRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login({ user_id: userId.trim(), password, ...(mfaCode ? { mfa_code: mfaCode.trim() } : {}) })
      // Refetch everything that failed with 401 while signed out.
      await qc.invalidateQueries()
      setPassword('')
      setMfaCode('')
      setNeedsMfa(false)
      onClose()
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err)
      if (/mfa_required/.test(msg)) {
        setNeedsMfa(true)
        setError('该账号已启用 MFA，请输入 6 位动态验证码。')
      } else if (err instanceof ApiError && err.status === 401) {
        setError('账号或密码不正确。')
      } else if (err instanceof ApiError && err.status === 404) {
        setError('该部署已关闭密码登录（DEV_LOGIN_ENABLED=false），请使用你的 OIDC 提供方登录。')
      } else if (err instanceof ApiError && err.status === 429) {
        setError('尝试次数过多，请等待一分钟后再试。')
      } else {
        setError(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-overlay" onClick={onClose}>
      <div
        className="auth-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-dialog-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="auth-dialog-title">登录 ForgeFlow</h2>
        <p className="auth-hint">
          本地开发登录（<code>POST /auth/login</code>，由 <code>DEV_LOGIN_ENABLED</code> 控制是否启用）。
          预置演示账号：<code>admin</code>、<code>manager-1</code>、<code>rep-1</code>、<code>viewer-1</code>
          —— 密码为 <code>.env</code> 中的 <code>DEV_LOGIN_PASSWORD</code>。生产部署请通过 OIDC 登录。
        </p>
        <form onSubmit={submit}>
          <label>
            <span>用户</span>
            <input
              ref={firstFieldRef}
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              autoComplete="username"
              required
            />
          </label>
          <label>
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          {needsMfa && (
            <label>
              <span>MFA 验证码</span>
              <input
                inputMode="numeric"
                pattern="[0-9]{6}"
                maxLength={6}
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                autoComplete="one-time-code"
              />
            </label>
          )}
          {error && (
            <p className="auth-error" role="alert">
              {error}
            </p>
          )}
          <div className="auth-actions">
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? '登录中…' : '登录'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/** Topbar auth control: "Sign in" when signed out; avatar + sign-out when in. */
export function AuthControls() {
  const session = useSession()
  const qc = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)

  // Let other surfaces (e.g. the signed-out banner) open the dialog.
  useEffect(() => {
    const open = () => setDialogOpen(true)
    window.addEventListener(OPEN_SIGNIN_EVENT, open)
    return () => window.removeEventListener(OPEN_SIGNIN_EVENT, open)
  }, [])

  const signOut = useCallback(async () => {
    await logout()
    // Drop cached data fetched under the old session.
    qc.clear()
  }, [qc])

  return (
    <>
      {session ? (
        <span className="auth-user">
          <span className="avatar" title={`已登录：${session.userId}（${session.role}）`}>
            {initials(session.userId)}
          </span>
          <span className="auth-role">{session.role}</span>
          <button type="button" className="btn sm ghost" onClick={signOut}>
            退出登录
          </button>
        </span>
      ) : (
        <button type="button" className="btn sm primary" onClick={() => setDialogOpen(true)}>
          登录
        </button>
      )}
      <SignInDialog open={dialogOpen} onClose={() => setDialogOpen(false)} />
    </>
  )
}

/** Slim banner under the topbar when browsing the console signed out. */
export function AuthBanner() {
  const session = useSession()
  if (session) return null
  return (
    <div className="auth-banner" role="status">
      <span>
        你尚未登录——实时面板无法加载数据（接口会返回 <code>401 missing bearer token</code>）。
      </span>
      <button type="button" className="btn sm" onClick={openSignIn}>
        登录
      </button>
    </div>
  )
}
