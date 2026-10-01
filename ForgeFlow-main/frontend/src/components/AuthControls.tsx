import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, login, logout } from '../api/client'
import { humanizeError } from '../api/errors'
import { useSession } from '../hooks/useSession'
import { OPEN_SIGNIN_EVENT, openSignIn } from './authEvents'
import { roleLabel } from '../i18n/labels'
import '../styles/auth.css'

/**
 * 预置演示账号 —— 以「中文角色名 + 账号」的形式给出。
 *
 * 背景 (INC 体检 F4)：这里原先直出 `POST /auth/login`、`.env`、`DEV_LOGIN_ENABLED`、
 * `DEV_LOGIN_PASSWORD` 等开发调试串。任何访客点顶栏「登录」都能看到这些内容，属于
 * 生产构建里的可见文案。账号本身是登录输入框的实际取值，必须保留；因此这里保留账号，
 * 但把它包在中文角色名里，并去掉全部接口路径与环境变量名。
 *
 * 角色中文名复用 `i18n/labels.ts` 的 `ROLE_LABELS`（管理员 / 经理 / 销售代表 / 只读访客）。
 */
const DEMO_ACCOUNTS: ReadonlyArray<{ role: string; userId: string }> = [
  { role: '管理员', userId: 'admin' },
  { role: '经理', userId: 'manager-1' },
  { role: '销售代表', userId: 'rep-1' },
  { role: '只读访客', userId: 'viewer-1' },
]

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
  // 后端**没有**暴露「是否启用口令登录」的查询端点（已核对 forgeflow/api/routers/auth.py，
  // 仅 /auth/login、/auth/refresh、/auth/logout、/auth/mfa/*、/auth/oidc/exchange、
  // /auth/introspect 六个；关闭口令登录时 /auth/login 会返回 404）。因此这里是唯一可用
  // 的前端信号：真的尝试过一次并被服务端以 404 拒了，才认定本部署未开放口令登录。
  const [pwdLoginOff, setPwdLoginOff] = useState(false)
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
        // 与第 145 行输入框标签「多因素验证码」保持中文一致。
        setError('该账号已启用多因素认证，请输入 6 位动态验证码。')
      } else if (err instanceof ApiError && err.status === 401) {
        setError('账号或密码不正确。')
      } else if (err instanceof ApiError && err.status === 404) {
        // 后端对本部署状态给出的唯一真实信号：口令登录未开放。
        setPwdLoginOff(true)
        setError('本部署未开放账号密码登录，请使用单点登录（OIDC）进入。')
      } else if (err instanceof ApiError && err.status === 429) {
        setError('尝试次数过多，请等待一分钟后再试。')
      } else {
        // 兜底：走统一的中文转译路径，不把服务端英文原始报文直接显示给访客。
        setError(humanizeError(err, '登录失败').label)
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
        {pwdLoginOff ? (
          /* 已由服务端确认本部署未开放口令登录 —— 只给单点登录引导。 */
          <p className="auth-hint">
            本部署未开放账号密码登录，请使用单点登录（OIDC）进入；入口地址请联系你的系统管理员。
          </p>
        ) : (
          /* 平实中文表述：无接口路径、无环境变量名。账号是登录输入的实际取值，必须保留。 */
          <p className="auth-hint">
            本演示环境支持账号密码登录。预置演示账号：
            {DEMO_ACCOUNTS.map((a, i) => (
              <span key={a.userId}>
                {i > 0 ? '、' : ''}
                <code>{`${a.role} · ${a.userId}`}</code>
              </span>
            ))}
            ，密码请向部署方索取。正式的生产部署请使用单点登录（OIDC）。
          </p>
        )}
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
              <span>多因素验证码</span>
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
          <span className="avatar" title={`已登录：${session.userId}（${roleLabel(session.role)}）`}>
            {initials(session.userId)}
          </span>
          <span className="auth-role">{roleLabel(session.role)}</span>
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
        你尚未登录——实时面板无法加载数据（接口会返回未授权错误 401）。
      </span>
      <button type="button" className="btn sm" onClick={openSignIn}>
        登录
      </button>
    </div>
  )
}
