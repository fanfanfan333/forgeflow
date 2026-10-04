/**
 * SkillMetricsPanel —— INC46 T36「指标与基准」页签（T06 三栏骨架上的新增页签）。
 *
 * 与 T14 **不重复建设**：T14 的 `SkillLayerNav` 是主体内的 L1/L2/L3 渐进披露，
 * 本组件是**资产中心级的度量页签**，聚合两条 T36 只读端点：
 *   · `GET /metrics/outcomes`          → 效果指标（`skill-metrics-*`）
 *   · `GET /metrics/benchmark/latest`  → 端到端基准（`skill-metrics-bench-*`）
 *
 * 诚实纪律（红线 4 / 12，**零编造**）
 * ----------------------------------
 * · 每个指标都是「**未测量 ⇒ `—`**」：后端返回 `null` ⇒ 渲染 `—`，**绝不 0 兜底**；
 * · `has_data === false`（0 个有标签 run）⇒ 全部指标一律 `—`，**严禁** `0%`；
 * · 基准语料哈希不符（`live.available === false`）⇒ 逐字展示原因，不渲染「全绿」；
 * · 未落库过（`has_persisted === false`）⇒ `latest` 段诚实空态，不补造运行记录。
 *
 * 既有 testid 与内容**一字不动**（红线：REMOVED=0）；本组件只**新增** testid。
 */

import { useState } from 'react'

import { humanizeError } from '../../api/errors'
import { useBenchmarkLatest, useMetricsOutcomes } from '../../api/hooks'
import type { BenchmarkCategoryRow } from '../../api/client'
import '../../styles/skill-metrics.css'

export type SkillMetricsTabKey = 'outcomes' | 'benchmark'

const TABS: Array<{ key: SkillMetricsTabKey; label: string; hint: string }> = [
  { key: 'outcomes', label: '效果指标', hint: '首过成功 / 采纳 / 复用 / 修复 / 成本 / 延迟' },
  { key: 'benchmark', label: '端到端基准', hint: '冻结语料（≥ 50 例）通过矩阵' },
]

/**
 * 指标展示表：`{键, 标签, 单位}`。顺序稳定；键与后端 `compute_all` 的**真源一致**。
 * `unit === 'rate'` ⇒ 百分号；`ms` / `tok` / `s` ⇒ 数值 + 单位。
 */
const METRIC_ROWS: Array<{ key: string; label: string; unit: 'rate' | 'ms' | 'tok' | 's' }> = [
  { key: 'first_pass_success', label: '首过成功率', unit: 'rate' },
  { key: 'adoption_rate', label: '采纳率', unit: 'rate' },
  { key: 'skill_reuse_rate', label: '技能复用率', unit: 'rate' },
  { key: 'self_repair_rate', label: '自修复率', unit: 'rate' },
  { key: 'clarification_rate', label: '澄清率', unit: 'rate' },
  { key: 'failure_report_rate', label: '失败上报率', unit: 'rate' },
  { key: 'rollback_rate', label: '回滚率', unit: 'rate' },
  { key: 'cost_per_task.tokens_per_task', label: '每任务 token', unit: 'tok' },
  { key: 'cost_per_task.seconds_per_task', label: '每任务耗时', unit: 's' },
  { key: 'latency_p50', label: '延迟 P50', unit: 'ms' },
  { key: 'latency_p95', label: '延迟 P95', unit: 'ms' },
]

/** 真实有限数 ⇒ 格式化；`null` / 非有限 ⇒「—」（**不是** 0）。 */
function metricText(value: number | null | undefined, unit: 'rate' | 'ms' | 'tok' | 's'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (unit === 'rate') return `${(value * 100).toFixed(1)}%`
  if (unit === 'ms') return `${value.toFixed(0)} ms`
  if (unit === 's') return `${value.toFixed(1)} s`
  return value.toFixed(0)
}

export function SkillMetricsPanel() {
  const [active, setActive] = useState<SkillMetricsTabKey>('outcomes')

  return (
    <section className="skill-metrics" data-testid="skill-metrics" data-active={active}>
      <div className="skill-metrics-tabs" role="tablist" aria-label="指标与基准">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            className={`skill-metrics-tab${active === tab.key ? ' active' : ''}`}
            data-testid="skill-metrics-tab"
            data-tab={tab.key}
            aria-selected={active === tab.key}
            title={tab.hint}
            onClick={() => setActive(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {active === 'outcomes' ? (
        <OutcomesTab />
      ) : (
        <BenchmarkTab />
      )}
    </section>
  )
}

/* ------------------------------------------------------------------------- *
 * 效果指标
 * ------------------------------------------------------------------------- */
function OutcomesTab() {
  const query = useMetricsOutcomes()

  if (query.isLoading) {
    return (
      <p className="skill-metrics-loading" data-testid="skill-metrics-loading">
        指标加载中…
      </p>
    )
  }
  if (query.isError) {
    return (
      <p className="skill-metrics-error" data-testid="skill-metrics-error" role="alert">
        {humanizeError(query.error, '指标加载失败').label}
      </p>
    )
  }

  const data = query.data
  if (!data) {
    return (
      <p className="skill-metrics-empty" data-testid="skill-metrics-empty">
        —
      </p>
    )
  }

  const hasData = data.has_data === true
  const measured = data.metrics ?? {}
  // 保真：表内 11 项按序渲染；后端若出现表外键，一并原样列出（不丢信息）。
  const knownKeys = new Set(METRIC_ROWS.map((row) => row.key))
  const extraKeys = Object.keys(measured).filter((key) => !knownKeys.has(key))

  return (
    <div className="skill-metrics-body" data-testid="skill-metrics-outcomes">
      <div className="skill-metrics-summary text-mono" data-testid="skill-metrics-summary">
        <span>
          有标签 run {hasData ? data.labeled_runs : '—'} / 窗口 run {data.total_runs}
        </span>
        <span className="skill-metrics-threshold">
          回退阈值 {data.regression_threshold_pp}pp
        </span>
      </div>

      {!hasData && (
        <p className="skill-metrics-empty" data-testid="skill-metrics-empty">
          0 个有标签 run ⇒ 全部指标「—」（未测量，不是 0）
        </p>
      )}

      <dl className="skill-metrics-list" data-testid="skill-metrics-metrics">
        {METRIC_ROWS.map((row) => (
          <div
            className="skill-metrics-row"
            key={row.key}
            data-testid="skill-metrics-metric"
            data-metric={row.key}
            data-measured={typeof measured[row.key] === 'number' ? 'true' : 'false'}
          >
            <dt>{row.label}</dt>
            <dd className="text-mono">
              {hasData ? metricText(measured[row.key], row.unit) : '—'}
            </dd>
          </div>
        ))}
        {extraKeys.map((key) => (
          <div
            className="skill-metrics-row"
            key={key}
            data-testid="skill-metrics-metric"
            data-metric={key}
            data-measured={typeof measured[key] === 'number' ? 'true' : 'false'}
          >
            <dt className="text-mono">{key}</dt>
            <dd className="text-mono">
              {hasData ? metricText(measured[key], 'tok') : '—'}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/* ------------------------------------------------------------------------- *
 * 端到端基准
 * ------------------------------------------------------------------------- */
function categoryEntries(
  byCategory: Record<string, BenchmarkCategoryRow> | undefined,
): Array<[string, BenchmarkCategoryRow]> {
  if (!byCategory) return []
  return Object.keys(byCategory)
    .sort()
    .map((name) => [name, byCategory[name]] as [string, BenchmarkCategoryRow])
}

function BenchmarkTab() {
  const query = useBenchmarkLatest()

  if (query.isLoading) {
    return (
      <p className="skill-metrics-loading" data-testid="skill-metrics-loading">
        基准加载中…
      </p>
    )
  }
  if (query.isError) {
    return (
      <p className="skill-metrics-error" data-testid="skill-metrics-error" role="alert">
        {humanizeError(query.error, '基准加载失败').label}
      </p>
    )
  }

  const data = query.data
  if (!data) {
    return (
      <p className="skill-metrics-empty" data-testid="skill-metrics-empty">
        —
      </p>
    )
  }

  const live = data.live
  const entries = categoryEntries(live.by_category)

  return (
    <div className="skill-metrics-body" data-testid="skill-metrics-bench">
      <div className="skill-metrics-summary text-mono" data-testid="skill-metrics-bench-corpus">
        <span title={data.corpus.frozen_hash}>
          冻结语料 {data.corpus.frozen_hash.slice(0, 12)}…
        </span>
        <span
          className={`skill-metrics-contract ${
            data.corpus.contract_satisfied ? 'ok' : 'bad'
          }`}
          data-satisfied={data.corpus.contract_satisfied ? 'true' : 'false'}
        >
          契约 {data.corpus.contract_satisfied ? '满足' : '未满足'}
        </span>
      </div>

      {!live.available ? (
        <p className="skill-metrics-error" data-testid="skill-metrics-bench-unavailable" role="alert">
          基准不可用：{live.error ?? '未知原因'}
        </p>
      ) : (
        <>
          <div className="skill-metrics-summary text-mono" data-testid="skill-metrics-bench-live">
            <span>
              {live.passed} / {live.total_cases} 通过
            </span>
            <span>失败 {live.failed}</span>
            <span>错误 {live.errors}</span>
            <span>跳过 {live.skipped}</span>
          </div>

          {entries.length > 0 ? (
            <ul className="skill-metrics-cats" data-testid="skill-metrics-bench-cats">
              {entries.map(([name, row]) => (
                <li
                  className="skill-metrics-cat"
                  key={name}
                  data-testid="skill-metrics-bench-cat"
                  data-category={name}
                  data-all-passed={row.total > 0 && row.passed === row.total ? 'true' : 'false'}
                >
                  <span className="skill-metrics-cat-name">{name}</span>
                  <span className="skill-metrics-cat-count text-mono">
                    {row.passed}/{row.total}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="skill-metrics-empty" data-testid="skill-metrics-empty">
              —
            </p>
          )}
        </>
      )}

      <div className="skill-metrics-persisted text-mono" data-testid="skill-metrics-bench-persisted">
        {data.has_persisted ? '已落库：最近一次运行见 benchmark_runs' : '尚未落库（nightly 未运行）'}
      </div>
    </div>
  )
}
