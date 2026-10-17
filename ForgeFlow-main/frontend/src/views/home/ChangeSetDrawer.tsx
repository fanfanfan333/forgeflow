/**
 * ChangeSetDrawer — Phase C「Change Set」抽屉（AUDIT §16.2 / §16.3）。
 *
 * **无新导航**（Sidebar 不动）：这是工作台 / Runs 内的抽屉，由右栏 Inspector 的
 * `changeset` 分支触发打开。数据 + 动作全部经 `useChangeSet`（只调既有端点）。
 *
 * 版面（§6.2）：顶栏「Change Set #id」+ 次行「N files changed · +a / -d」+ 文件列表 +
 * 文件详情（modified before/after = 统一 diff 逐行）+ 修改原因 + Agent 说明 +
 * validation + test + 三动作（应用修改 / 拒绝 / 继续修改）。
 *
 * 诚实（§16.2.3 / §16.7.10）：无真源字段**显式**呈现（「未记录修改原因」/
 * 「不可验证（无 validation 端点）」/「—」），**绝不**用 0 / 「通过」冒充。
 * diff 行来自 `useChangeSet.diff`（`realRun.ts::deriveCodeDiff`）——**唯一真源**，
 * 抽屉**不**重解析 diff 文本。
 *
 * testid 一律新增 `change-set-*`（只增不改不删）。
 */
import { useEffect, useState } from 'react'
import { artifactFinalAnswer } from '../runs/realRun'
import { changeSetTotals, versionRegisteredText, type ChangeSet } from './changeSet'
import { useChangeSet } from './useChangeSet'
import '../../styles/change-set.css'

type ChangeSetDrawerProps = {
  /** 目标运行（`null` ⇒ 抽屉内显诚实空态）。 */
  runId: string | null
  /** 是否打开。 */
  open: boolean
  /** 关闭抽屉。 */
  onClose: () => void
}

export function ChangeSetDrawer({ runId, open, onClose }: ChangeSetDrawerProps) {
  const ctrl = useChangeSet(open ? runId : null)

  // Esc 关闭（无障碍：模态抽屉）。这是**外部事件订阅**，符合 effect 语义。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const cs = ctrl.changeSet
  return (
    <div className="change-set-backdrop" data-testid="change-set-backdrop" onClick={onClose}>
      <div
        className="change-set-drawer"
        data-testid="change-set-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={cs ? `Change Set #${cs.id}` : '变更集'}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="change-set-header" data-testid="change-set-header">
          <div>
            <h2 className="change-set-title" data-testid="change-set-title">
              {cs ? `Change Set #${cs.id}` : 'Change Set'}
            </h2>
            <p className="change-set-subtitle" data-testid="change-set-subtitle">
              {cs ? changeSetSummaryLine(cs) : '暂无变更集'}
            </p>
          </div>
          <button
            type="button"
            className="btn ghost sm change-set-close"
            data-testid="change-set-close"
            aria-label="关闭变更集"
            onClick={onClose}
          >
            关闭
          </button>
        </header>

        {ctrl.loading ? (
          <p className="change-set-empty" data-testid="change-set-empty">
            加载中…
          </p>
        ) : ctrl.error ? (
          <p className="change-set-error" role="alert">
            {ctrl.error}
          </p>
        ) : !cs ? (
          <p className="change-set-empty" data-testid="change-set-empty">
            本次运行没有可呈现的变更集（非代码修改任务，或尚未产生 diff）。
          </p>
        ) : (
          // `key={cs.id}`：切换运行 ⇒ 重挂载 ⇒ 文件选择态自然复位（**不用** effect
          // 同步 setState，避免 `react-hooks/set-state-in-effect`）。
          <ChangeSetBody
            key={cs.id}
            cs={cs}
            diffFiles={ctrl.diff?.files ?? []}
            diffText={ctrl.diff?.text ?? ''}
            agentNote={ctrl.detail ? artifactFinalAnswer(ctrl.detail) : ''}
          />
        )}

        {cs && (
          <ChangeSetActions
            pending={ctrl.pending}
            actionError={ctrl.actionError}
            onApply={ctrl.apply}
            onReject={ctrl.reject}
            onReanalyze={ctrl.reanalyze}
          />
        )}
      </div>
    </div>
  )
}

/**
 * 次行摘要。
 *   · 代码面：「N files changed · +a / -d」（数字来自**真实** unified diff 统计）。
 *   · 文档面：段级 diff 非行数口径 ⇒ 只显「N files changed」（**不**臆造 ± 行号）。
 */
function changeSetSummaryLine(cs: ChangeSet): string {
  const t = changeSetTotals(cs)
  if (cs.source === 'document') {
    return t.files > 0 ? `${t.files} files changed` : '无文件变更'
  }
  if (!cs.diffPresent && t.files === 0) return '无代码变更'
  return `${t.files} files changed · +${t.additions} / -${t.deletions}`
}

function ChangeSetBody({
  cs,
  diffFiles,
  diffText,
  agentNote,
}: {
  cs: ChangeSet
  diffFiles: { path: string; additions: number; deletions: number; lines: { kind: string; text: string }[] }[]
  diffText: string
  agentNote: string
}) {
  // 当前选中文件（**本组件内部**态；`ChangeSetBody` 由父级以 `key={cs.id}` 重挂载，
  // 故切换运行时自动复位为 0，无需 effect 同步）。
  const [selectedFile, setSelectedFile] = useState(0)
  const active = diffFiles.length > 0 ? diffFiles[Math.min(selectedFile, diffFiles.length - 1)] : null
  return (
    <>
      <section className="change-set-section">
        <h3 className="change-set-section-title">任务诉求</h3>
        <p className="change-set-note" data-testid="change-set-task">
          {cs.task.trim() || '—'}
        </p>
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">修改文件</h3>
        {cs.files.length === 0 ? (
          <p className="change-set-empty" data-testid="change-set-files-empty">
            无可列文件
          </p>
        ) : (
          <ul className="change-set-files">
            {cs.files.map((f, i) => (
              <li key={f.path}>
                <button
                  type="button"
                  className="change-set-file"
                  data-testid="change-set-file"
                  aria-current={i === Math.min(selectedFile, cs.files.length - 1) ? 'true' : undefined}
                  onClick={() => setSelectedFile(i)}
                >
                  <span className="change-set-file-path">{f.path}</span>
                  {/* 代码面：真实 unified diff 逐文件 ±；文档面：段级 diff 非行数口径。 */}
                  {cs.source === 'document' ? (
                    <span className="change-set-file-stat">段级 diff</span>
                  ) : (
                    <span className="change-set-file-stat">
                      +{f.additions} / -{f.deletions}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">
          {cs.source === 'document' ? 'Diff（段级 · 见版本链）' : '统一 Diff（修改前 / 修改后）'}
        </h3>
        {cs.source === 'document' ? (
          // 文档面无 unified 文本（段级 diff 由版本链承载）⇒ 诚实空态，**不**伪造 unified。
          <p className="change-set-empty" data-testid="change-set-diff-empty">
            文档面为段级 diff（段落 / 单元格），由版本链承载；本抽屉不呈现 unified 文本。
          </p>
        ) : active ? (
          <pre className="change-set-diff" data-testid="change-set-diff">
            {active.lines.map((line, i) => (
              <span
                key={i}
                className={`change-set-diff-line ${line.kind}`}
                data-testid="change-set-diff-line"
              >
                {line.text}
              </span>
            ))}
          </pre>
        ) : diffText.trim() ? (
          <pre className="change-set-diff" data-testid="change-set-diff">
            {diffText}
          </pre>
        ) : (
          <p className="change-set-empty" data-testid="change-set-diff-empty">
            无 diff（未产生变更文本）
          </p>
        )}
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">修改原因</h3>
        <p className="change-set-note" data-testid="change-set-reason">
          {cs.reason ?? '未记录修改原因'}
        </p>
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">Agent 说明</h3>
        <p className="change-set-note" data-testid="change-set-agent-note">
          {agentNote.trim() || '本次无 Agent 说明'}
        </p>
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">Validation</h3>
        <ValidationBlock cs={cs} />
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">测试</h3>
        <TestBlock cs={cs} />
      </section>

      <section className="change-set-section">
        <h3 className="change-set-section-title">风险与审批</h3>
        <dl className="change-set-facts">
          <Fact
            label="风险等级"
            // 代码面：codeplane 不带 risk_level（真源在 GET /approvals）⇒ 诚实「—」；
            // 文档面：无风险概念 ⇒ 「不适用（文档面）」。
            value={cs.source === 'document' ? '不适用（文档面）' : cs.riskLevel ?? '—'}
          />
          <Fact label="是否需要 HITL" value={cs.needsHitl ? '需要' : '否'} />
          <Fact label="审批状态" value={cs.approvalStatus !== '' ? cs.approvalStatus : '未决'} />
          <Fact label="是否已应用" value={cs.applied ? '已应用' : '未应用'} />
          {cs.source === 'document' && (
            // §16.13(4) —— 版本登记行（诚实三分支，附真实失败原因）。
            <Fact label="版本登记" value={versionRegisteredText(cs)} />
          )}
        </dl>
      </section>
    </>
  )
}

function ValidationBlock({ cs }: { cs: ChangeSet }) {
  if (cs.source === 'document') {
    // §16.2.3：文档面无 validation 概念 ⇒ 显式「不适用」，**不**冒充「通过」。
    return (
      <p className="change-set-empty" data-testid="change-set-validation">
        不适用（文档面）
      </p>
    )
  }
  if (!cs.validation) {
    return (
      <p className="change-set-empty" data-testid="change-set-validation">
        不可验证（无 validation 机制）
      </p>
    )
  }
  const { validation: v } = cs
  return (
    <dl className="change-set-facts" data-testid="change-set-validation">
      <Fact
        label="已跑校验"
        value={v.ran.length > 0 ? v.ran.join('、') : '未跑校验工具'}
      />
      <Fact label="语法 / 类型 / 构建" value="不可验证（无 validation 端点）" />
      {v.note && <Fact label="备注" value={v.note} />}
    </dl>
  )
}

function TestBlock({ cs }: { cs: ChangeSet }) {
  if (cs.source === 'document') {
    // §16.2.3：文档面无 test 概念 ⇒ 显式「不适用」，**不**写 0。
    return (
      <p className="change-set-empty" data-testid="change-set-test">
        不适用（文档面）
      </p>
    )
  }
  if (!cs.test) {
    return (
      <p className="change-set-empty" data-testid="change-set-test">
        不适用（非代码）
      </p>
    )
  }
  const t = cs.test
  if (!t.measured) {
    return (
      <p className="change-set-empty" data-testid="change-set-test">
        未测量（本次未产出可解析的测试结论）
      </p>
    )
  }
  return (
    <dl className="change-set-facts" data-testid="change-set-test">
      <Fact label="结论" value={t.verdict} />
      <Fact label="通过 / 失败" value={`${t.passed} / ${t.failed}`} />
      {t.failedCases.length > 0 && <Fact label="失败用例" value={t.failedCases.join('、')} />}
      {t.command.trim() !== '' && <Fact label="命令" value={t.command} />}
    </dl>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="change-set-fact">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

function ChangeSetActions({
  pending,
  actionError,
  onApply,
  onReject,
  onReanalyze,
}: {
  pending: 'approve' | 'reject' | 'replan' | null
  actionError: string | null
  onApply: () => void
  onReject: () => void
  onReanalyze: () => void
}) {
  const busy = pending !== null
  return (
    <div className="change-set-actions" data-testid="change-set-actions">
      <button
        type="button"
        className="btn primary sm"
        data-testid="change-set-apply"
        disabled={busy}
        onClick={onApply}
      >
        {pending === 'approve' ? '应用中…' : '应用修改'}
      </button>
      <button
        type="button"
        className="btn ghost sm"
        data-testid="change-set-reject"
        disabled={busy}
        onClick={onReject}
      >
        {pending === 'reject' ? '拒绝中…' : '拒绝'}
      </button>
      <button
        type="button"
        className="btn ghost sm"
        data-testid="change-set-reanalyze"
        disabled={busy}
        onClick={onReanalyze}
      >
        {pending === 'replan' ? '重分析中…' : '继续修改'}
      </button>
      {actionError && (
        <p className="change-set-error" role="alert" data-testid="change-set-action-error">
          {actionError}
        </p>
      )}
    </div>
  )
}
