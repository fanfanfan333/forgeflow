export function ChatAvatar({ user = false }: { user?: boolean }) {
  return <span className={`chat-avatar chat-avatar-${user ? 'user' : 'agent'}`} aria-label={user ? '用户头像' : 'AI机器人头像'}>
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {user ? <><circle cx="12" cy="8" r="3.5" /><path d="M5 21v-2a7 7 0 0 1 14 0v2" /></> : <><rect x="4" y="7" width="16" height="13" rx="4" /><path d="M12 3v4M2 12v4M22 12v4M9 16h6" /><circle cx="9" cy="12" r=".7" /><circle cx="15" cy="12" r=".7" /></>}
    </svg>
  </span>
}
