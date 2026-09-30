/**
 * INC25 / T05 —— 代码任务的「审批块」：代码变更（Diff）+ 测试结论 + **仅三个**动作
 * （批准修改 / 拒绝 / 重新分析，AC-18）。
 *
 * 诚实纪律（与仓库同款）：
 *   * Diff 逐字呈现（解析成按文件分组只是为了可读，行文本一字不改）；**无变更** ⇒
 *     诚实空态，绝不编一张 Diff（AC-15）。
 *   * 测试结论的判定权在 ForgeFlow 侧：`measured === false` ⇒ 「未测量」，**绝不**
 *     默认判「通过」（AC-16）。
 *   * 三个动作**都真调后端** —— 批准 / 拒绝走 `/codeplane/runs/{id}/approve|reject`，
 *     重新分析复用既有 `POST /runs/{id}/replan`（都不是占位按钮）。
 */
import type { CodeApproval as CodeApprovalState, CodeDiff, CodeTestResult } from './types'

/** 测试结论 → 业务表述（未知值原样呈现，不臆造）。 */
function verdictLabel(verdict: string): string {
  switch ((verdict ?? '').toLowerCase()) {
    case 'passed':
      return '通过'
    case 'failed':
      return '未通过'
    case 'unmeasured':
      return '未测量'
    default:
      return verdict || '未测量'
  }
}

/** 审批状态 → 业务表述。 */
function approvalLabel(status: string): string {
  switch ((status ?? '').toLowerCase()) {
    case 'pending':
      return '等待人工审批'
    case 'approved':
      return '已批准'
    case 'rejected':
      return '已拒绝'
    default:
      return status ? status : '未记录'
  }
}

export function CodeApproval({
  approval,
  diff,
  tests,
  onDecision,
  onReanalyze,
  pending,
  error,
}: {
  approval: CodeApprovalState
  diff: CodeDiff
  tests: CodeTestResult
  /** 批准 / 拒绝（真调后端；由页面层注入）。 */
  onDecision: (action: 'approve' | 'reject') => void
  /** 第三动作「重新分析」（复用既有 `POST /runs/{id}/replan`）。 */
  onReanalyze: () => void
  pending: boolean
  error: string | null
}) {
  // 已决（批准/拒绝）后，批准 / 拒绝两个入口禁用（仍是**存在**的入口，AC-18 计数不变）。
  const decided = approval.status === 'approved' || approval.status === 'rejected'

  return (
    <section className="code-approval" data-testid="code-approval" aria-label="代码变更审批">
      <div className="code-approval-head">
        <h4 className="code-sect-head">代码变更审批</h4>
        <span className={`badge ${decided ? 'amber' : ''}`.trim()}>
          {approvalLabel(approval.status)}
        </span>
      </div>

      {/* ── 代码变更（Diff，按文件分组；逐字，无改动则诚实空态）────────────── */}
      {/* INC29 / T03 —— 补 `id` 供三入口「查看 Diff」定位（`data-testid` 不动）。 */}
      <div className="code-diff" id="code-diff" data-testid="code-diff">
        {diff.present ? (
          diff.files.length > 0 ? (
            <div className="code-diff-files">
              {diff.files.map((f) => (
                <div className="code-diff-file" key={f.path}>
                  <div className="code-diff-file-head">
                    <span className="code-diff-path mono">{f.path}</span>
                    <span className="code-diff-stat">
                      <span className="code-diff-add num">+{f.additions}</span>
                      <span className="code-diff-rem num">-{f.deletions}</span>
                    </span>
                  </div>
                  <pre className="code-diff-pre">
                    {f.lines.map((l, i) => (
                      <span
                        className={`diff-line ${l.kind === 'ctx' ? '' : l.kind}`.trim()}
                        key={`${f.path}-${i}`}
                      >
                        {l.text}
                        {'\n'}
                      </span>
                    ))}
                  </pre>
                </div>
              ))}
            </div>
          ) : (
            // 无法按文件分组（无 `diff --git` 头）⇒ 原样呈现全文，不改写。
            <pre className="code-diff-pre">{diff.text}</pre>
          )
        ) : (
          <p className="res-subtle" data-testid="code-diff-empty">
            本次无代码变更
          </p>
        )}
      </div>

      {/* ── 测试结论（ForgeFlow 侧判定；未测量 ≠ 通过）──────────────────────── */}
      {/* INC29 / T03 —— 补 `id` 供三入口「查看测试」定位（`data-testid` 不动）。 */}
      <div className="code-tests" id="code-tests" data-testid="code-tests">
        {tests.measured ? (
          <p className="code-tests-line">
            测试结论：{verdictLabel(tests.verdict)} · {tests.passed} 通过 / {tests.failed} 未通过 /{' '}
            {tests.errors} 错误
          </p>
        ) : (
          <p className="code-tests-line">测试结果未测量（输出不可解析或不适用）</p>
        )}
        {tests.command && <p className="code-tests-cmd mono">测试命令：{tests.command}</p>}
        {tests.failedCases.length > 0 && (
          <ul className="code-tests-cases">
            {tests.failedCases.map((c) => (
              <li className="mono" key={c}>
                {c}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ── 仅三个动作入口（AC-18）────────────────────────────────────────── */}
      <div className="code-approval-actions">
        <button
          type="button"
          className="btn primary"
          data-testid="code-approve"
          disabled={pending || decided}
          onClick={() => onDecision('approve')}
        >
          批准修改
        </button>
        <button
          type="button"
          className="btn danger"
          data-testid="code-reject"
          disabled={pending || decided}
          onClick={() => onDecision('reject')}
        >
          拒绝
        </button>
        <button
          type="button"
          className="btn ghost"
          data-testid="code-reanalyze"
          disabled={pending}
          onClick={onReanalyze}
        >
          重新分析
        </button>
      </div>

      {pending && (
        <p className="af-note" role="status">
          正在处理审批决定，请稍候…
        </p>
      )}
      {error && (
        <p className="af-note warn" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
