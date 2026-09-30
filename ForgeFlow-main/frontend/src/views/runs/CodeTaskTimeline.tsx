/**
 * INC25 / T05 —— 代码任务的「任务时间线」（AC-13 / AC-14）。
 *
 * 可见行**只**出三样东西：
 *   * 一个状态图标 —— 经 `stepStatusToStageStatus` 映射（复用既有步骤状态词表，
 *     **不**新增第二套词汇；`blocked` 仍是「受阻」而非失败）；
 *   * 业务文案 —— 后端事件的 `label`（**绝不**用原始工具名 / Action / Observation）；
 *   * 实测耗时 —— 未测量渲染「—」，**绝不** `0 ms`（AC-14）。
 *
 * 原始 `tool` / `detail` 一律进**默认折叠**的 `<details data-testid="code-trace">`
 * （AC-13）：折叠由原生 `<details>` 负责，本组件与 CSS **都不给** `<details>` 及其
 * 直接子节点写 `display`（见 runs.css 的折叠纪律，与 `ResultNextActions` 的
 * `.res-more` 同款口径）。
 *
 * INC29 / T03（§11④）—— 时间线区域新增**一行三入口控件**：「查看 Diff」「查看测试」
 * 「查看 Trace」。三者都是**真入口**（真滚动 / 真展开），不是装饰按钮：
 *   * 「查看 Diff」⇒ `scrollIntoView` 到 `CodeApproval.tsx::#code-diff`；
 *   * 「查看测试」⇒ `scrollIntoView` 到 `CodeApproval.tsx::#code-tests`；
 *   * 「查看 Trace」⇒ **先**把本组件 `#code-trace`（原生 `<details>`）的 `open`
 *     置真展开，**再**滚动到它 —— 否则滚到一个折叠起来的盒子上等于没定位。
 * 定位逻辑抽成模块级纯函数（`revealTrace` / `scrollToId`），点击处理只做薄封装；
 * 目标缺失一律安全 no-op（不抛），便于在 node 里独立真跑（机械证据）。
 */
import type { CodeInjectedContext, CodeTimelineItem, RunStageStatus } from './types'
import { stepStatusToStageStatus } from './realRun'

/**
 * INC29 / T03 —— 滚动到元素（`scrollIntoView`）。目标缺失 / 引擎未实现滚动 ⇒ 返回
 * `false`（安全 no-op，绝不抛）。
 */
function scrollTo(el: Element | null): boolean {
  if (!el) return false
  const target = el as HTMLElement
  if (typeof target.scrollIntoView !== 'function') return false
  target.scrollIntoView({ behavior: 'smooth', block: 'start' })
  return true
}

/** 滚动到 `id` 对应的元素（供「查看 Diff」/「查看测试」使用）。 */
export function scrollToId(doc: Document, id: string): boolean {
  return scrollTo(doc.getElementById(id))
}

/**
 * 「查看 Trace」：先把 `#code-trace`（原生 `<details>`）展开（`open = true`），再滚动
 * 过去。目标缺失 ⇒ `false`。展开走 `open` 属性（原生 details 驱动折叠，见折叠纪律）。
 */
export function revealTrace(doc: Document): boolean {
  const el = doc.getElementById('code-trace') as HTMLDetailsElement | null
  if (!el) return false
  el.open = true
  return scrollTo(el)
}

/** 状态 → 圆点色调（既有 oklch token 变量，不新增设计系统）。 */
const STATUS_TONE: Record<RunStageStatus, string> = {
  pending: 'na',
  running: 'running',
  done: 'done',
  paused: 'paused',
  failed: 'failed',
  blocked: 'blocked',
  na: 'na',
}

/**
 * 实测耗时。未测量（`null` 或非正数）⇒「—」，**绝不** 0 —— 0 会读成「测过且没花
 * 时间」，正是本增量要移除的假象（AC-14）。
 */
function fmtMs(ms: number | null): string {
  if (ms === null || ms <= 0) return '—'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`
}

/**
 * INC28 / W7 —— 「本次注入」：平台为这个代码任务选中的 Skill / Memory。
 *
 * 诚实纪律：两者皆空 ⇒ 整段**不进 DOM**（`present` 由调用方判定），**绝不**补占位。
 * 只出后端 `codeplane.injected` 的逐字字段（skill：`name`/`version`；memory：`id`/`scope`）。
 * 纯手写 CSS（见 `runs.css` 的 `.code-injected*`），**不**引入 MUI / Tailwind。
 */
function CodeInjected({ injected }: { injected: CodeInjectedContext }) {
  return (
    <div className="code-injected" data-testid="code-injected">
      <h5 className="code-injected-head">本次注入</h5>
      {injected.skills.length > 0 && (
        <p className="code-injected-row">
          <span className="code-injected-kind">技能</span>
          {injected.skills.map((s, i) => (
            <span className="code-injected-chip" key={`sk-${s.id || s.name}-${i}`}>
              <span className="code-injected-name">{s.name || s.id}</span>
              {s.version ? (
                <span className="code-injected-meta mono">v{s.version}</span>
              ) : null}
            </span>
          ))}
        </p>
      )}
      {injected.memory.length > 0 && (
        <p className="code-injected-row">
          <span className="code-injected-kind">记忆</span>
          {injected.memory.map((m, i) => (
            <span className="code-injected-chip" key={`mem-${m.id}-${i}`}>
              <span className="code-injected-name mono">{m.id}</span>
              {m.scope ? <span className="code-injected-meta mono">{m.scope}</span> : null}
            </span>
          ))}
        </p>
      )}
    </div>
  )
}

export function CodeTaskTimeline({
  timeline,
  affectedSteps,
  injected,
}: {
  timeline: CodeTimelineItem[]
  /** 受影响步骤（降级时点名；空数组不渲染该行）。业务名，逐字后端字段。 */
  affectedSteps: string[]
  /** 本次注入的 Skill / Memory（两者皆空 ⇒ 整段不渲染）。逐字后端字段。 */
  injected: CodeInjectedContext
}) {
  const hasInjected = injected.skills.length > 0 || injected.memory.length > 0

  // INC29 / T03 —— 三入口一行（真定位，见模块级 `scrollToId` / `revealTrace`）。
  // 无论时间线是否为空都渲染：三入口是「代码任务卡片」的固定控件（§11④）。
  const entries = (
    <div className="code-entries" data-testid="code-entries" role="group" aria-label="代码任务入口">
      <button
        type="button"
        className="code-entry-btn"
        data-testid="code-entry-diff"
        onClick={() => scrollToId(document, 'code-diff')}
      >
        查看差异
      </button>
      <button
        type="button"
        className="code-entry-btn"
        data-testid="code-entry-tests"
        onClick={() => scrollToId(document, 'code-tests')}
      >
        查看测试
      </button>
      <button
        type="button"
        className="code-entry-btn"
        data-testid="code-entry-trace"
        onClick={() => revealTrace(document)}
      >
        查看轨迹
      </button>
    </div>
  )

  if (timeline.length === 0) {
    return (
      <section className="code-timeline" data-testid="code-timeline" aria-label="任务时间线">
        <h4 className="code-sect-head">任务时间线</h4>
        {entries}
        <p className="res-subtle">本次代码任务没有可展示的执行步骤。</p>
        {affectedSteps.length > 0 && (
          <p className="code-affected">受影响步骤：{affectedSteps.join('、')}</p>
        )}
        {hasInjected && <CodeInjected injected={injected} />}
      </section>
    )
  }

  return (
    <section className="code-timeline" data-testid="code-timeline" aria-label="任务时间线">
      <h4 className="code-sect-head">任务时间线</h4>
      {entries}
      <ol className="code-tl-list">
        {timeline.map((it, i) => {
          const stage = stepStatusToStageStatus(it.status)
          return (
            <li className="code-tl-item" data-status={stage} key={`${it.seq}-${it.ts}-${i}`}>
              <span className={`code-dot ${STATUS_TONE[stage]}`} aria-hidden="true" />
              {/* 业务文案：**只**显示后端 label；label 为空也用中性说明，绝不回落工具名。 */}
              <span className="code-tl-label">{it.label || '（该步未标注）'}</span>
              <span
                className="code-tl-ms num"
                title={it.latencyMs === null ? '该步骤未测量耗时' : undefined}
              >
                {fmtMs(it.latencyMs)}
              </span>
            </li>
          )
        })}
      </ol>

      {/* INC28 / W7 —— 本次注入（Skill / Memory）；两者皆空 ⇒ 不进 DOM。 */}
      {hasInjected && <CodeInjected injected={injected} />}

      {/* 原始轨迹：默认折叠（无 `open`）；原始 tool / detail 只在这里出现（AC-13）。 */}
      {/* INC29 / T03 —— 补 `id` 供「查看 Trace」定位（`data-testid` 不动）。 */}
      <details className="code-trace" id="code-trace" data-testid="code-trace">
        <summary className="code-trace-sum">查看原始执行轨迹（原文）</summary>
        {/* 直接子节点**不写 `display`** —— 见文件头折叠纪律。 */}
        <div className="code-trace-body">
          {timeline.map((it, i) => (
            <div className="code-trace-item" key={`raw-${it.seq}-${it.ts}-${i}`}>
              <div className="code-trace-head">
                <span className="code-trace-seq mono">#{it.seq}</span>
                {it.kind && <span className="code-trace-kind mono">{it.kind}</span>}
                {it.tool && <span className="code-trace-tool mono">{it.tool}</span>}
                <span className="code-trace-status mono">{it.status}</span>
              </div>
              {it.detail ? <pre className="code-trace-detail">{it.detail}</pre> : null}
            </div>
          ))}
        </div>
      </details>
    </section>
  )
}
