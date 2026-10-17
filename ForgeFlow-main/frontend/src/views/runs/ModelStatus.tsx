/**
 * ModelStatus — 模型只读入口（INC35 · 规格 §21/§22；INC55 缺陷② 扩展）。
 *
 * **只读**：展示当前运行真实使用的 Provider 与模型名，以及本地 Ollama 的运行状态。
 * 不做切换（规格 §21「前端模型选择只作为配置入口」；切换需要后端 Provider 契约，
 * 属独立增量）。
 *
 * 诚实纪律（规格 §22 · INC55 缺陷② 追加）：
 *   · 有模型身份 ⇒ 显示 `Ollama · qwen3:8b` 这类真实组合。
 *   · **没有**本次运行的模型记录（`llm` 为 null / undefined / 空对象 `{}`）⇒
 *     默认显示「暂无运行记录，模型状态未知」，不猜。
 *   · `platformFallback` 开启（首页 composer）时，上一条可回退显示**平台配置**的
 *     模型，并**显式**标注「平台配置」且 `title` 说明「未记录本次运行实际使用的
 *     模型」—— 绝不写成「已连接」，也绝不暗示该模型被本次运行用过。
 */
import { usePlatformModelStatus, useRunDetail } from '../../api/hooks'
import type { RunLLM } from '../../api/client'
import { deriveModelInfo, derivePlatformModelInfo } from './modelInfo'
import { IconBot } from '../../components/icons'

type Props = {
  /** 直接给 `llm` 时不再取数（父组件已有运行详情时用）。 */
  llm?: RunLLM | null
  /** 没有 `llm` 时按 runId 取真实运行详情。 */
  runId?: string | null
  mode?: 'concise' | 'debug'
  /**
   * INC55 缺陷② —— 允许在「没有本次运行记录」时回退显示**平台配置**的模型。
   * 默认 `false`（`LiveRunsView` 面向某条具体 run，不适用）。开启方由此获得一个
   * 可区分语义的展示（带「平台配置」标签），而非一个假模型名或「已连接」。
   */
  platformFallback?: boolean
}

export function ModelStatus({
  llm,
  runId,
  mode = 'concise',
  platformFallback = false,
}: Props) {
  // `llm === undefined` ⇒ 需要自己取数；显式传 `null` 表示「父组件确认无数据」。
  const needFetch = llm === undefined
  const q = useRunDetail(needFetch ? runId ?? null : null)
  const effective: RunLLM | null | undefined = needFetch ? q.data?.llm ?? null : llm
  const info = deriveModelInfo(effective)

  // INC55 缺陷②：仅当「无本次运行记录」且调用方允许回退时才取平台**配置**模型
  //（否则 `enabled:false`，不发请求）。
  const wantPlatform = platformFallback && info === null
  const platform = usePlatformModelStatus(wantPlatform)
  const platformInfo = derivePlatformModelInfo(platform.data)

  // 取数中：给骨架，不先给结论。
  if (needFetch && q.isPending && runId) {
    return (
      <span className="model-status" data-testid="model-status">
        <span className="model-dot unknown" aria-hidden="true" />
        <span className="model-text">模型状态读取中…</span>
      </span>
    )
  }

  if (!info) {
    // 平台配置模型可用 ⇒ 回退显示（语义**可区分**：标签 + title 双标注）。
    if (platformInfo) {
      const base =
        `平台配置的模型（未记录本次运行实际使用的模型）：` +
        `${platformInfo.provider} · ${platformInfo.model}`
      const title =
        mode === 'debug' && platformInfo.baseUrl ? `${base}（${platformInfo.baseUrl}）` : base
      return (
        <span
          className="model-status platform"
          data-testid="model-status"
          data-available="true"
          data-source="platform"
          title={title}
        >
          <span className="model-dot unknown" aria-hidden="true" />
          <IconBot width={12} height={12} className="model-ico" />
          <span className="model-text">
            {platformInfo.provider}
            <span className="model-sep"> · </span>
            <span className="mono">{platformInfo.model}</span>
            <span className="model-configured">（平台配置）</span>
          </span>
        </span>
      )
    }

    // 平台配置仍在读取中 ⇒ 继续给骨架，不先下结论。
    if (wantPlatform && platform.isPending) {
      return (
        <span className="model-status" data-testid="model-status">
          <span className="model-dot unknown" aria-hidden="true" />
          <span className="model-text">模型状态读取中…</span>
        </span>
      )
    }

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
