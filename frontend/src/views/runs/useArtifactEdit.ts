/**
 * INC24 / F5 —— 产物编辑态 hook（**共享**，由 `ResultPanel` 常驻持有）。
 *
 * 为什么提升为 hook（C6 / R4，这是上一轮踩过的坑）：
 *   * 旧实现把编辑态埋在 `ArtifactsBlock` **内部**，而四个 Tab 面板用 `hidden` 控制显隐
 *     ⇒ **切 Tab 不卸载面板**，内部 state 会「静默失效」；且段⑤「我自己处理」与段⑥的
 *     `result-edit/save/cancel` 需要**共享同一个** `editing` 才能联动。
 *   * 现提升为 hook，由 `ResultPanel`（常驻，不随 Tab 卸载）持有，段⑤与段⑥**共用**它。
 *
 * 换 run 自动复位（对齐既有注释「切 Tab 不卸载面板，内部 state 会静默失效」的教训）：
 *   * 内部状态以 `scope = "{runId}::{primary.id}"` 打标；读取时若 `scope` 不匹配当前
 *     渲染的 scope ⇒ **直接取新默认值**（`editing=false`、`saved=primary.content`）。
 *   * 因此**无需 `useEffect`、无需 `key` 强制重挂载** —— 换 run（或换主产物）自动复位，
 *     不会跨 run 泄漏编辑内容，也不会出现 setState-in-effect。
 *
 * 写回纪律（**不撒谎**）：
 *   * 「保存」**只在本地生效**（`saved`），**不**写回服务端 —— 文案如实说明。
 *   * 「存入知识库」是**唯一**真调后端的动作（`POST /memory/store`）；成功/失败**如实**回报。
 *   * 「导出 / 复制 / 打印」分别是纯前端下载 / 剪贴板 / 浏览器打印；被浏览器拒绝时如实说。
 */
import { useState } from 'react'
import { useStoreMemory } from '../../api/hooks'
import { humanizeError } from '../../api/errors'
import type { RunArtifact } from '../../api/client'
import { copyText, downloadTextFile, printResult, resultMarkdownFilename } from './resultActions'

/** 段⑤ / 段⑥ 共享的产物编辑态（组件只读它，不各自持有）。 */
export type ArtifactEdit = {
  /** 是否处于编辑态（进编辑态时读视图旁出现编辑器）。 */
  editing: boolean
  /** 当前草稿（编辑态文本框绑定它）。 */
  draft: string
  /** 已确认的正文（读视图/导出/复制/打印/存入都用它；初值 = `primary.content`）。 */
  saved: string
  /** 本地保存结果提示（未修改 / 不能为空 / 已保存仅本地）。 */
  saveNote: string | null
  /** 存入知识库的结果提示（成功含记忆 ID / 失败原因）。 */
  storeNote: string | null
  /** 导出 PDF（打印）的结果提示。 */
  printNote: string | null
  /** 复制全文的结果提示。 */
  copyNote: string | null
  /**
   * 导出 Markdown 的结果提示（**新增**）——「导出」是纯前端下载，本身**没有**网络请求，
   * 若不回一条可见确认，它在 AC-5 口径下会被判成「点击后无网络请求也无可见状态变化」的
   * 死按钮。故导出后**如实**回报文件名（这是真的发生了的事，不是装饰文案）。
   */
  exportNote: string | null
  /** 「存入知识库」是否进行中（按钮禁用态）。 */
  storePending: boolean
  setDraft: (value: string) => void
  startEdit: () => void
  cancelEdit: () => void
  saveEdit: () => void
  exportResult: () => void
  copyResult: () => void
  exportPdf: () => void
  saveToMemory: () => void
}

/** 内部状态：以 `scope` 打标，读取时按当前 scope 判定是否复位。 */
type EditState = {
  scope: string
  saved: string
  draft: string
  on: boolean
  saveNote: string | null
  storeNote: string | null
  printNote: string | null
  copyNote: string | null
  exportNote: string | null
}

/**
 * 产物编辑态 hook。
 *
 * @param runId   当前运行 id（换 run ⇒ 自动复位）。
 * @param primary 主产物（`pickPrimaryArtifact(artifacts)`）；为 `null` 时 `saved=''`。
 */
export function useArtifactEdit(runId: string, primary: RunArtifact | null): ArtifactEdit {
  const scope = `${runId}::${primary?.id ?? ''}`
  const base = primary?.content ?? ''
  const fresh = (): EditState => ({
    scope,
    saved: base,
    draft: base,
    on: false,
    saveNote: null,
    storeNote: null,
    printNote: null,
    copyNote: null,
    exportNote: null,
  })

  const [state, setState] = useState<EditState>(fresh)
  // scope 不匹配 ⇒ 用新默认值渲染（换 run 自动复位）；不改写 state、无 setState-in-effect。
  const cur = state.scope === scope ? state : fresh()
  // 变更一律经此：先按当前 scope 归一（必要时复位），再合并补丁。
  const patch = (p: Partial<EditState>) =>
    setState((s) => ({ ...(s.scope === scope ? s : fresh()), ...p }))

  // 「存入知识库」= 唯一真调后端的能力（POST /memory/store）。
  const storeMemory = useStoreMemory()

  const startEdit = () => patch({ on: true, draft: cur.saved, saveNote: null })
  const cancelEdit = () => patch({ on: false, draft: cur.saved, saveNote: null })
  const setDraft = (value: string) => patch({ draft: value })

  const saveEdit = () => {
    if (cur.draft.trim() === '') {
      // 空内容拦截，且**不进入**只读（用户仍在编辑）。
      patch({ on: true, saveNote: '结果正文不能为空' })
      return
    }
    if (cur.draft === cur.saved) {
      // 无变更时**不得**报告「已保存」。
      patch({ on: false, saveNote: '内容未修改，未保存' })
      return
    }
    patch({ on: false, saved: cur.draft, saveNote: '已保存（仅本地，未写回服务端）' })
  }

  // 导出 / 复制 / 打印用 `saved`（**完整原文**，含平台执行账本），故需传完整 `primary.content`。
  const exportResult = () => {
    const filename = resultMarkdownFilename(runId)
    downloadTextFile(filename, cur.saved)
    // 纯前端下载没有网络请求 ⇒ 如实回一条可见确认（否则该按钮在 AC-5 口径下是「死按钮」）。
    patch({ exportNote: `已导出完整原文：${filename}` })
  }

  const copyResult = async () => {
    const ok = await copyText(cur.saved)
    patch({ copyNote: ok ? '已复制结果全文' : '复制未成功：浏览器未授予剪贴板权限' })
  }

  const exportPdf = () =>
    patch({
      printNote: printResult()
        ? '已调起打印（可在打印对话框选择“另存为 PDF”）'
        : '当前环境不支持打印',
    })

  const saveToMemory = () => {
    if (!primary?.content || storeMemory.isPending) return
    patch({ storeNote: null })
    storeMemory.mutate(
      {
        content: primary.content,
        namespace: 'global/agent-deliverables',
        metadata: { source_run_id: runId, source_artifact_ref: primary.result_ref ?? '' },
      },
      {
        onSuccess: (res) =>
          patch({ storeNote: `已存入知识库（记忆 ID ${res.memory_id.slice(0, 8)}）` }),
        onError: (err) =>
          patch({ storeNote: `存入未成功：${humanizeError(err, '存入知识库失败').label}` }),
      },
    )
  }

  return {
    editing: cur.on,
    draft: cur.draft,
    saved: cur.saved,
    saveNote: cur.saveNote,
    storeNote: cur.storeNote,
    printNote: cur.printNote,
    copyNote: cur.copyNote,
    exportNote: cur.exportNote,
    storePending: storeMemory.isPending,
    setDraft,
    startEdit,
    cancelEdit,
    saveEdit,
    exportResult,
    copyResult,
    exportPdf,
    saveToMemory,
  }
}
