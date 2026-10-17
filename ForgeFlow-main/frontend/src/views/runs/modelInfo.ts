/**
 * modelInfo — 从**运行详情**推导当前模型身份（INC35 · 规格 §21/§22）。
 *
 * 数据来源是**真实**的：`GET /runs/{id}`.llm 即后端
 * `orchestrator.py::_build_planner_models()` 产出的 `llm_runtime`，其中
 * `models[]` 由 `llm_planner.describe_model()` 生成（`slot` / `class` / `llm_type` /
 * `model` / `base_url`）。这是后端用来**自证「真的建出了配置的 provider，没有静默降级」**
 * 的同一份证据，前端只是把它读出来，**不新增后端契约、不猜模型名**。
 *
 * 诚实边界：
 *   · 没有任何模型身份 ⇒ 返回 `available:false` + 诚实原因（含后端 `degraded` 词表），
 *     **绝不**静默假装有模型。
 *   · Provider 名由 `base_url` / `llm_type` / `class` 推断；推断不出时给「兼容模型服务」，
 *     不冒认成某个具体厂商。
 */
import type { PlatformModelStatus, RunLLM } from '../../api/client'

export type ModelInfo = {
  /** 后端是否真的构建出了模型实例。 */
  available: boolean
  /** 业务语 Provider 名（Ollama / OpenAI 兼容 / …）。 */
  provider: string
  /** 模型名（后端 `describe_model().model`）。 */
  model: string
  /** 服务端点 —— 工程值，只在 debug 档展示。 */
  baseUrl: string
  /** 槽位（`strong` / `worker`）—— 工程值，只在 debug 档展示。 */
  slot: string
  /** `available:false` 时的诚实原因（业务语）。 */
  reason: string
}

type RawModel = { slot?: unknown; class?: unknown; llm_type?: unknown; model?: unknown; base_url?: unknown }

/** 后端 `llm.degraded` 词表 → 业务语原因（与 `realRun.deriveDegradeNotice` 同一口径）。 */
export function degradeReason(raw: string): string {
  if (raw === 'no_model') return '模型不可用'
  if (raw === 'provider_degraded_to_mock') return '模型调用未成功，已降级'
  if (raw.startsWith('exception:')) return '模型调用失败'
  return '模型不可用'
}

function providerLabel(entry: RawModel): string {
  const hay = `${entry.llm_type ?? ''} ${entry.class ?? ''} ${entry.base_url ?? ''}`.toLowerCase()
  if (hay.includes('11434') || hay.includes('ollama')) return 'Ollama'
  if (hay.includes('openai') || hay.includes('azure')) return 'OpenAI 兼容'
  if (hay.includes('anthropic') || hay.includes('claude')) return 'Anthropic'
  return '兼容模型服务'
}

/** `llm` 缺失 ⇒ `null`（调用方据此渲染「暂无数据」的诚实空态）。 */
export function deriveModelInfo(llm: RunLLM | undefined | null): ModelInfo | null {
  if (!llm || typeof llm !== 'object') return null
  // INC55 缺陷② —— 空对象 `{}` 是后端的**诚实「无记录」**：`GET /runs/{id}` 对
  // 未保留执行明细的 run 返回 `dict(getattr(record, "llm", None) or {})`，即 `{}`
  // （无任何键）。它**不是**「有记录但 models 为空」。三者（null / undefined /
  // `{}`）必须同走「无记录」空态，绝不落入下方「有记录但 models 为空」的
  // 「模型：未记录」分支 —— 否则界面会把「没有记录」谎报成「记录了一个空模型」。
  if (Object.keys(llm as Record<string, unknown>).length === 0) return null
  const raw = (llm as { models?: unknown }).models
  const models: RawModel[] = Array.isArray(raw) ? (raw as RawModel[]) : []
  const degraded = typeof llm.degraded === 'string' ? llm.degraded : ''

  if (models.length === 0) {
    return {
      available: false,
      provider: '',
      model: '',
      baseUrl: '',
      slot: '',
      reason: degraded ? degradeReason(degraded) : '模型：未记录',
    }
  }

  const picked = models.find((m) => m?.slot === 'strong') ?? models[0]
  const model = typeof picked?.model === 'string' ? picked.model : ''
  const slot = typeof picked?.slot === 'string' ? picked.slot : ''
  if (!model) {
    return {
      available: false,
      provider: '',
      model: '',
      baseUrl: '',
      slot,
      reason: degraded ? degradeReason(degraded) : '模型：未记录',
    }
  }
  return {
    available: true,
    provider: providerLabel(picked),
    model,
    baseUrl: typeof picked?.base_url === 'string' ? picked.base_url : '',
    slot,
    reason: '',
  }
}

/**
 * INC55 缺陷② —— 从 `GET /runs/model-status`（平台**配置**的模型）推导身份。
 *
 * 仅在「最新 run 无记录」时由 `ModelStatus` 回退使用；它表示的是**平台配置**，
 * 与「本次运行实际使用的模型」是**不同语义**，调用方必须显式区分（不得写成
 * 「已连接」，也不得暗示该模型被本次运行用过）。配置不可描述（`models` 为空）
 * ⇒ `null`（诚实未知），**绝不**用假模型名兜底。
 */
export function derivePlatformModelInfo(
  resp: PlatformModelStatus | undefined | null,
): ModelInfo | null {
  if (!resp || typeof resp !== 'object') return null
  const models = Array.isArray(resp.models) ? (resp.models as RawModel[]) : []
  if (models.length === 0) return null
  // 复用**同一** `deriveModelInfo`：平台响应带的 `models` 与 run 的 `llm.models`
  // 同形（slot / llm_type / model / base_url），故 provider/模型名口径完全一致。
  const info = deriveModelInfo({ models } as unknown as RunLLM)
  if (!info || !info.available) return null
  return info
}
