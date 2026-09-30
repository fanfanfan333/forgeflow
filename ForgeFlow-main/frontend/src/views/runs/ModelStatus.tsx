/**
 * ModelStatus — 模型只读入口（INC35 · 规格 §21/§22）。
 *
 * **只读**：展示当前运行真实使用的 Provider 与模型名，以及本地 Ollama 的运行状态。
 * 不做切换（规格 §21「前端模型选择只作为配置入口」；切换需要后端 Provider 契约，
 * 属独立增量）。
 *
 * 诚实纪律（规格 §22）：
 *   · 有模型身份 ⇒ 显示 `Ollama · qwen3:8b` 这类真实组合。
 *   · 没有模型身份 ⇒ 显示后端给出的**真实原因**（「模型不可用」等），
 *     **绝不**静默切换成一个假模型名。
 *   · 还没跑过任何任务 ⇒ 显示「暂无运行记录，模型状态未知」，不猜。
 */
import { useRunDetail } from '../../api/hooks'
import type { RunLLM } from '../../api/client'
import { deriveModelInfo } from './modelInfo'
import { IconBot } from '../../components/icons'

type Props = {
  /** 直接给 `llm` 时不再取数（父组件已有运行详情时用）。 */
  llm?: RunLLM | null
  /** 没有 `llm` 时按 runId 取真实运行详情。 */
  runId?: string | null
  mode?: 'concise' | 'debug'
}

export function ModelStatus({ llm, runId, mode = 'concise' }: Props) {
  // `llm === undefined` ⇒ 需要自己取数；显式传 `null` 表示「父组件确认无数据」。
  const needFetch = llm === undefined
  const q = useRunDetail(needFetch ? runId ?? null : null)
  const effective: RunLLM | null | undefined = needFetch ? q.data?.llm ?? null : llm

  // 取数中：给骨架，不先给结论。
  if (needFetch && q.isPending && runId) {
    return (
      <span className="model-status" data-testid="model-status">
        <span className="model-dot unknown" aria-hidden="true" />
        <span className="model-text">模型状态读取中…</span>
      </span>
    )
  }

  const info = deriveModelInfo(effective)

  if (!info) {
    return (
      <span
        className="model-status"
        data-testid="model-status"
        title="尚未产生任何运行，平台还不知道本次使用哪个模型"
      >
        <span className="model-dot unknown" aria-hidden="true" />
        <span className="model-text">暂无运行记录，模型状态未知</span>
      </span>
    )
  }

  if (!info.available) {
    return (
      <span
        className="model-status unavailable"
        data-testid="model-status"
        data-available="false"
        title={info.reason}
      >
        <span className="model-dot off" aria-hidden="true" />
        <span className="model-text">{info.reason}</span>
      </span>
    )
  }

  const title =
    mode === 'debug'
      ? `${info.provider} · ${info.model}（槽位 ${info.slot || '—'} · ${info.baseUrl || '端点未记录'}）`
      : `${info.provider} · ${info.model}`

  return (
    <span className="model-status" data-testid="model-status" data-available="true" title={title}>
      <span className="model-dot on" aria-hidden="true" />
      <IconBot width={12} height={12} className="model-ico" />
      <span className="model-text">
        {info.provider}
        <span className="model-sep"> · </span>
        <span className="mono">{info.model}</span>
      </span>
      {mode === 'debug' && info.baseUrl && <span className="model-debug mono">{info.baseUrl}</span>}
    </span>
  )
}
