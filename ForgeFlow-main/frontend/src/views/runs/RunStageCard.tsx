/**
 * A single execution stage = one collapsible card (design §4).
 *
 * Accessibility contract:
 *  - the header is a native <button type="button"> (Enter/Space work for free);
 *  - `aria-expanded` toggles, `aria-controls` points at the detail `id`;
 *  - collapsed detail carries `hidden`, so its controls leave the tab order;
 *  - focus stays on the trigger after expanding (no focus stealing).
 */
import { useState } from 'react'
import { IconAgents, IconCheck, IconChevronDown, IconCompass, IconDocument, IconMemory } from '../../components/icons'
import type { RunSourceKind, RunStage, RunStageStatus, ViewMode } from './types'

const STATUS_META: Record<RunStageStatus, { label: string; tone: string }> = {
  pending: { label: '待执行', tone: '' },
  running: { label: '进行中', tone: 'blue' },
  done: { label: '已完成', tone: 'emerald' },
  paused: { label: '已暂停', tone: 'amber' },
  failed: { label: '失败', tone: 'red' },
  // INC15 — a declared step whose required input is missing (NOT a failure).
  blocked: { label: '受阻', tone: 'blocked' },
  // INC15 — a candidate step that does not apply to this task.
  na: { label: '未适用', tone: 'na' },
}

const SOURCE_ICON: Record<RunSourceKind, typeof IconCompass> = {
  web: IconCompass,
  doc: IconDocument,
  crm: IconAgents,
  memory: IconMemory,
}

/**
 * Human duration. A real sub-second measurement stays in milliseconds
 * (「6 ms」) — rendering it as `0.0s` would read as "zero time" for a value that
 * is not zero. `null`/`0` both mean the duration was not measured → 「—」
 * (never `0 ms`); a paused step shows 「~ pending ~」.
 */
function formatDuration(ms: number | null, status: RunStageStatus): string {
  if (status === 'paused') return '~ pending ~'
  if (ms === null || ms === 0) return '—'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`
}

/**
 * Tool latency. The runtime DOES meter per-tool wall time
 * (``tool_executor._elapsed_ms``) and keeps sub-millisecond precision, so a real
 * value like ``0.062`` renders ``0.062 ms``. ``null`` (or ``0``) only means the
 * measurement is missing (a blocked / unavailable / refused step is never
 * measured) — render 「—」 rather than `0 ms`, which would claim a measurement
 * that was never taken. The 「—」 carries a `title` so a reader knows *why* the
 * cell is empty rather than reading it as a broken character.
 */
function formatMs(ms: number | null): string {
  return ms !== null && ms > 0 ? `${ms} ms` : '—'
}

/**
 * INC17 — render a round's (model-supplied) args as a readable ``key=value`` line.
 *
 * Values are stringified defensively: a string is shown verbatim, everything else
 * via ``JSON.stringify`` (an object/array is not flattened into ``[object Object]``).
 * An empty args object is an honest「（无参数）」rather than a blank cell.
 */
function formatArgs(args: Record<string, unknown>): string {
  const keys = Object.keys(args ?? {})
  if (keys.length === 0) return '（无参数）'
  return keys
    .map((k) => {
      const v = args[k]
      const shown = typeof v === 'string' ? v : JSON.stringify(v)
      return `${k}=${shown}`
    })
    .join('，')
}

export function RunStageCard({ stage, mode }: { stage: RunStage; mode: ViewMode }) {
  const [open, setOpen] = useState(false)
  const triggerId = `${stage.id}-trigger`
  const detailId = `${stage.id}-detail`
  const status = STATUS_META[stage.status]
  const hasDetail =
    stage.sources.length > 0 ||
    stage.memories.length > 0 ||
    stage.tools.length > 0 ||
    !!stage.round ||
    (mode === 'debug' && !!stage.raw)

  return (
    <article className="stage-card" data-status={stage.status}>
      <button
        type="button"
        className="stage-head"
        id={triggerId}
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="stage-order" data-testid="workchain-node-order">{stage.order}</span>
        <span className="stage-name" data-testid="workchain-node-name">{stage.name}</span>
        <span className={`badge ${status.tone}`.trim()} data-testid="workchain-node-status">{status.label}</span>
        <span className="stage-summary" data-testid="workchain-node-summary">{stage.summary}</span>
        <span className="stage-chev" aria-hidden="true">
          <IconChevronDown width={10} height={10} />
        </span>
      </button>

      <div id={detailId} className="stage-detail" role="region" aria-labelledby={triggerId} hidden={!open}>
        <dl className="stage-facts">
          <div>
            <dt>智能体</dt>
            <dd className="mono">{stage.agent}</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd className="num">{formatDuration(stage.durationMs, stage.status)}</dd>
          </div>
          <div>
            <dt>结果</dt>
            <dd>{stage.summary}</dd>
          </div>
          {/* INC17 — the ReAct round's own detail (参数 / 返回 / 模型说明), rendered
              inside the EXISTING facts list. Absent on pre-INC17 runs, so nothing
              changes for an old run; no new component, no new data-testid. */}
          {stage.round && (
            <>
              <div>
                <dt>参数</dt>
                <dd className="mono">{formatArgs(stage.round.args)}</dd>
              </div>
              <div>
                <dt>返回</dt>
                <dd>{stage.round.resultSnippet || '（无返回）'}</dd>
              </div>
              <div>
                <dt>模型说明</dt>
                {/* 真相优先：多数模型在「发起工具调用」的那一轮 content 为空
                    （qwen3 即如此）。写「无说明」会读成模型没思考，写成
                    「仅发起工具调用」才是它真正发生的事。 */}
                <dd>
                  {stage.round.modelText || '（该轮模型仅发起了工具调用，未返回文本）'}
                </dd>
              </div>
            </>
          )}
        </dl>

        {!hasDetail && <p className="af-note">该阶段尚无更多细节。</p>}

        {stage.sources.length > 0 && (
          <section className="stage-sect" aria-label="信息来源">
            <h4>信息来源</h4>
            <div className="ff-list">
              {stage.sources.map((s, i) => {
                const Icon = SOURCE_ICON[s.kind]
                return (
                  <div className="ff-row" key={`${s.kind}-${i}`}>
                    <span className="kind" aria-hidden="true">
                      <Icon width={13} height={13} />
                    </span>
                    <span className="label">{s.label}</span>
                    {s.detail && <span className="detail">{s.detail}</span>}
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {stage.memories.length > 0 && (
          <section className="stage-sect" aria-label="使用的记忆">
            <h4>使用的记忆</h4>
            <div className="ff-list">
              {stage.memories.map((m) => (
                <div className="mem-row" key={m.id}>
                  <div className="top">
                    <span className="ns">
                      {m.namespace} · {m.kind} · {m.date}
                    </span>
                    <span className="sim">{m.similarity.toFixed(2)}</span>
                  </div>
                  <div className="snippet">{m.snippet}</div>
                </div>
              ))}
            </div>
          </section>
        )}

        {stage.tools.length > 0 && (
          <section className="stage-sect" aria-label="调用的工具">
            <h4>调用的工具</h4>
            <div className="ff-list">
              {stage.tools.map((t, i) => {
                // A missing measurement (never metered) explains itself on hover
                // rather than reading as a broken 「—」 (honesty rule P0-2).
                const latencyTitle = t.ms === null ? '该步骤未测量耗时' : undefined
                return (
                  <div className={`ff-row${t.ok ? '' : ' bad'}`} key={`${t.name}-${i}`}>
                    <span className="kind" aria-hidden="true">
                      <IconCheck width={13} height={13} />
                    </span>
                    {mode === 'debug' ? (
                      <>
                        <span className="label mono">{t.name}</span>
                        <span className="detail" title={latencyTitle}>
                          {t.note ? `${t.note} · ` : ''}
                          {formatMs(t.ms)}
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="label">{t.bizLabel}</span>
                        <span className="detail" title={latencyTitle}>
                          {formatMs(t.ms)}
                        </span>
                      </>
                    )}
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {mode === 'debug' && stage.raw && (
          <div className="stage-raw">
            {stage.raw.model && (
              <span>
                model=<b>{stage.raw.model}</b>
              </span>
            )}
            {typeof stage.raw.tokens === 'number' && (
              <span>
                tokens=<b>{stage.raw.tokens.toLocaleString()}</b>
              </span>
            )}
            {typeof stage.raw.costCny === 'number' && (
              <span>
                cost=<b>¥{stage.raw.costCny.toFixed(4)}</b>
              </span>
            )}
            {typeof stage.raw.checkpoint === 'number' && (
              <span>
                checkpoint=<b>#{stage.raw.checkpoint}</b>
              </span>
            )}
          </div>
        )}
      </div>
    </article>
  )
}
