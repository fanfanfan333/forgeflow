/**
 * AgentStep — 一条 Agent 执行步骤（INC35 · 规格 §7）。
 *
 * 参考 `browser-use/chat-ui-example` 的 `step-section.tsx`：状态圆点（✓ / ✕ / ●）
 * + 业务标题 + 可折叠详情。默认密度下**只**出现业务语；原始工具 id、事件名、
 * 序号等工程值只在 `debug` 档出现（规格 §7「普通用户简单，开发/管理员完整」）。
 *
 * 与聊天消息的区别（规格 §7 的核心要求）：步骤**不是**聊天气泡，而是带状态圆点的
 * 执行条目，且默认折叠细节 —— 用户点「查看执行详情」才展开。
 */
import { useState } from 'react'
import {
  IconChart,
  IconCheck,
  IconChevronDown,
  IconClose,
  IconDocument,
  IconSearch,
  IconSparkle,
  IconTerminal,
  IconTools,
} from '../../components/icons'
import type { ReactNode } from 'react'
import type { StepStatus, ToolKind } from './toolLabels'
import { stepStatusLabel } from './toolLabels'

export type AgentStepData = {
  key: string
  /** 业务语标题（后端 note 或映射表，见 toolLabels）。 */
  title: string
  status: StepStatus
  kind: ToolKind
  /** 展开后的详情（观察结果摘要 / 受阻原因）；可为空。 */
  detail: string
  /** 工程值（原始工具 id / 事件名 / 序号）—— 仅 debug 档渲染。 */
  debug: string
}

const KIND_ICON: Record<ToolKind, ReactNode> = {
  research: <IconSearch width={12} height={12} />,
  data: <IconChart width={12} height={12} />,
  code: <IconTerminal width={12} height={12} />,
  report: <IconDocument width={12} height={12} />,
  skill: <IconSparkle width={12} height={12} />,
  tool: <IconTools width={12} height={12} />,
}

function StatusDot({ status }: { status: StepStatus }) {
  if (status === 'done') {
    return (
      <span className="step-dot done" aria-hidden="true">
        <IconCheck width={11} height={11} />
      </span>
    )
  }
  if (status === 'error' || status === 'blocked') {
    return (
      <span className={`step-dot ${status}`} aria-hidden="true">
        <IconClose width={11} height={11} />
      </span>
    )
  }
  return (
    <span className="step-dot running" aria-hidden="true">
      <i />
    </span>
  )
}

export function AgentStep({ step, mode }: { step: AgentStepData; mode: 'concise' | 'debug' }) {
  const [open, setOpen] = useState(false)
  const expandable = Boolean(step.detail) || mode === 'debug'

  return (
    <li className="agent-step" data-testid="workspace-live-step" data-status={step.status}>
      <button
        type="button"
        className="agent-step-head"
        onClick={() => expandable && setOpen((v) => !v)}
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
      >
        <StatusDot status={step.status} />
        <span className={`step-kind k-${step.kind}`} aria-hidden="true">
          {KIND_ICON[step.kind]}
        </span>
        <span className="agent-step-title">{step.title}</span>
        {step.status !== 'running' && (
          <span className={`agent-step-status s-${step.status}`}>{stepStatusLabel(step.status)}</span>
        )}
        {expandable && (
          <IconChevronDown
            width={13}
            height={13}
            className={`agent-step-chev${open ? ' open' : ''}`}
          />
        )}
      </button>

      {open && expandable && (
        <div className="agent-step-body">
          {step.detail && <p className="agent-step-detail">{step.detail}</p>}
          {mode === 'debug' && <p className="agent-step-debug mono">{step.debug}</p>}
        </div>
      )}
    </li>
  )
}
