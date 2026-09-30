/**
 * ThinkingIndicator — Agent 正在做什么（INC35 · 规格 §9）。
 *
 * 参考 `browser-use/chat-ui-example` 的 `thinking-indicator.tsx`：**三点跳动 + 文案**，
 * 而不是一个无限转圈。文案必须是**语义化**的（正在理解任务 / 正在检索知识 /
 * 正在调用技能 / 正在生成结果），让用户知道 Agent 此刻在做什么。
 *
 * 本组件是纯展示：`label` 由调用方（`WorkspaceLiveStrip`）从**真实事件**推导，
 * 组件自身不猜、不轮播、不假装进度。
 */
export function ThinkingIndicator({ label }: { label: string }) {
  return (
    <span className="think" data-testid="workspace-thinking" role="status" aria-live="polite">
      <span className="think-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span className="think-label">{label}</span>
    </span>
  )
}
