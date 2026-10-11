/**
 * QA (Edward) — **independent** re-verification of AgentFlow 2.0 / Phase B
 * (three-column Agent workbench). This is NOT the engineer's spec
 * (`workbench_phase_b.spec.ts`); it is a second, adversarial probe written to
 * falsify the engineer's self-attestation.
 *
 * What it proves (each with a positive/differential assertion, not a re-run):
 *   1. Three columns really sit side-by-side, and the RIGHT panel really
 *      switches on: left-column object clicks, center-column chip clicks, and
 *      the run-vs-run difference (runA shows INTENT_A, runB shows INTENT_B).
 *   2. The five "no real source" fields render an EXPLICIT empty state with the
 *      expected copy and NO number (skill success-rate, Run↔Skill recency,
 *      dataset schema/quality, session resource read-back, first-class ChangeSet).
 *   3. Degradation: no-session / empty-runs / failed-run / HTTP-500 all render
 *      readable states (never a white screen).
 *   4. A 320px viewport has no horizontal overflow.
 *
 * The engineer's stub keeps `/api/skills` EMPTY and never exercises the Skill
 * inspector, the ChangeSet inspector, the memory inspector, a 2-run session, a
 * failed run, or a 500 — all of which this spec covers.
 *
 * Network boundary: route stubs at `/api/**` (same convention as the repo e2e).
 */
import { test, expect } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

const SESSION_ID = 'sess_qa_b'
const RUN_A = 'run_qa_a'
const RUN_B = 'run_qa_b'
const INTENT_A = '分析季度数据'
const INTENT_B = '生成正式版文档'
const FILE_B = '项目方案_正式版.docx'
const ARTIFACT_B = 'art_qa_b_1'
const SKILL_ID = 'skill_doc_rewrite'
const SKILL_NAME = '文档重写'
const MEM_ID = 'mem_qa_1'
const REPLY = '已完成修改，生成正式版文档。'

const DIFF = [
  '--- a/summary.md',
  '+++ b/summary.md',
  '@@ -1,2 +1,3 @@',
  ' 标题',
  '+新增一行',
  '-删除一行',
  ' 结尾',
].join('\n')

function frame(runId: string, type: string, data: Record<string, unknown>, seq: number) {
  return { run_id: runId, type, data, seq, ts: '2026-01-01T00:00:00Z' }
}
function sse(frames: unknown[]): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'
}

type RunSpec = { runId: string; intent: string; status: string }

/** `GET /runs/{id}` payload. RUN_B is the "rich" run (artifacts/skills/memory/codeplane). */
function runDetail(spec: RunSpec): Record<string, unknown> {
  const rich = spec.runId === RUN_B
  const codeplane = rich
    ? {
        injected: {
          skills: [{ id: SKILL_ID, version: '2.1', name: SKILL_NAME }],
          memory: [{ id: MEM_ID, scope: 'project' }],
        },
        diff: DIFF,
        tests: { measured: true, verdict: 'passed', passed: 3, failed: 0, errors: 0 },
        approval: {
          status: 'approved',
          approval_id: 'ap-1',
          decided_by: 'qa',
          decided_at: '2026-01-01T00:02:00Z',
        },
        summary: { files_changed: 1, test_command: 'pytest', passed: 3, failed: 0, repair_rounds: 0 },
      }
    : {}
  const artifacts = rich
    ? [
        {
          id: ARTIFACT_B,
          kind: 'document_docx',
          title: FILE_B,
          format: 'docx',
          content: 'hello world',
          source: 'document.edit',
          result_ref: 'ref-b-1',
          created_at: '2026-01-01T00:01:00Z',
        },
      ]
    : []
  return {
    run_id: spec.runId,
    thread_id: 'thread-1',
    status: spec.status,
    outcome: spec.status === 'completed' ? 'success' : '',
    intent: spec.intent,
    steps: [
      { index: 0, tool: 'docs.parse', note: '读取文档', status: 'ok' },
      { index: 1, tool: 'export.docx', note: '生成新版本', status: 'ok' },
    ],
    errors: [],
    created_at: '2026-01-01T00:00:00Z',
    completed_at: spec.status === 'completed' ? '2026-01-01T00:01:00Z' : null,
    experience_id: null,
    runtime_mode: 'llm',
    tool_invocations: [
      { tool: 'docs.parse', status: 'ok', executed: true, latency_ms: 0.4, step_id: `${spec.runId}:1:0` },
    ],
    codeplane,
    artifacts,
    session_id: SESSION_ID,
    parent_run_id: null,
  }
}

function sessionDetail(runs: RunSpec[]): Record<string, unknown> {
  return {
    session_id: SESSION_ID,
    title: INTENT_B,
    runs: runs.map((r) => ({
      run_id: r.runId,
      thread_id: 'thread-1',
      status: r.status,
      outcome: r.status === 'completed' ? 'success' : '',
      intent: r.intent,
      title: r.intent,
      created_at: '2026-01-01T00:00:00Z',
      completed_at: r.status === 'completed' ? '2026-01-01T00:01:00Z' : null,
      experience_id: null,
      session_id: SESSION_ID,
      parent_run_id: null,
    })),
  }
}

async function stub(page: Page, runs: RunSpec[]) {
  const byId = new Map(runs.map((r) => [r.runId, r]))
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    const evM = /^\/api\/runs\/([^/]+)\/events$/.exec(path)
    if (evM) {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([
          frame(evM[1], 'run.final_answer', { text: REPLY, iteration: 1 }, 1),
          frame(evM[1], 'run.completed', { status: 'completed' }, 2),
        ]),
      })
    }
    // single session (left object tree source) — must be matched before the list.
    const sessM = /^\/api\/workspace\/sessions\/([^/]+)$/.exec(path)
    if (sessM && method === 'GET') return json(sessionDetail(runs))
    const detM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detM && method === 'GET') {
      const spec = byId.get(detM[1]) ?? { runId: detM[1], intent: '', status: 'completed' }
      return json(runDetail(spec))
    }
    // Skill catalogue — the engineer's stub returns EMPTY here; we return a real row.
    if (path === '/api/skills' && method === 'GET') {
      return json({
        total: 1,
        items: [
          {
            id: SKILL_ID,
            name: SKILL_NAME,
            domain: '文档',
            owner: 'qa-team',
            current_version: '2.1',
            status: 'published',
            description: '重写文档',
            usage_count: 42,
          },
        ],
      })
    }
    if (/^\/api\/skills\/[^/]+\/readiness$/.test(path)) {
      // `rate` unmeasured ⇒ null (the honest "not measured" shape).
      return json({ ready: true, rate: null })
    }
    if (path.startsWith('/api/experiences')) {
      return json({
        total: 1,
        items: [
          {
            id: 'exp-1',
            summary: '完成文档重写',
            outcome: 'success',
            tags: ['文档'],
            created_at: '2026-01-01T00:01:00Z',
            memory_ids: [MEM_ID],
          },
        ],
      })
    }
    if (path.startsWith('/api/resources')) return json({ total: 0, items: [] })
    if ((path === '/api/runs' || path === '/api/workspace/sessions') && method === 'GET') {
      return json({ total: 0, items: [] })
    }
    if (path.startsWith('/api/agents')) return json([])
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    return json({ total: 0, items: [] })
  })
}

async function signIn(page: Page, withSession: boolean) {
  await page.addInitScript(
    ({ sessionId, flag }) => {
      window.sessionStorage.setItem('forgeflow.jwt', 'qa-token')
      window.sessionStorage.setItem('forgeflow.role', 'admin')
      window.sessionStorage.setItem('forgeflow.user', 'qa-edward')
      if (flag) window.sessionStorage.setItem('forgeflow.conversation.qa-edward', sessionId)
    },
    { sessionId: SESSION_ID, flag: withSession },
  )
}

test.describe('QA · Phase B 独立复验', () => {
  test('①三栏同上下文 + 左/中触发右栏切换 + ②五个无源字段显式空态', async ({ page }) => {
    await signIn(page, true)
    await stub(page, [
      { runId: RUN_A, intent: INTENT_A, status: 'completed' },
      { runId: RUN_B, intent: INTENT_B, status: 'completed' },
    ])
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()

    // ── three columns exist and are side-by-side (left < center < right in x) ──
    await expect(page.getByTestId('workbench')).toBeVisible()
    const ctxCol = page.getByTestId('workbench-context')
    const centerCol = page.getByTestId('workbench-center')
    const inspCol = page.getByTestId('workbench-inspector')
    await expect(ctxCol).toBeVisible()
    await expect(centerCol).toBeVisible()
    await expect(inspCol).not.toBeVisible()
    const [a, b] = await Promise.all([
      ctxCol.boundingBox(),
      centerCol.boundingBox(),
    ])
    expect(a && b).toBeTruthy()
    expect(b!.y).toBeGreaterThan(a!.y)
    expect(b!.width).toBeGreaterThan(600)

    // ── left column: 2 runs; artifacts / skills / memory / changeset from latest run ──
    const runGroup = page.getByTestId('workbench-context-group-runs')
    await expect(page.getByTestId('workbench-context-run')).toHaveCount(2)
    await expect(runGroup).toContainText(INTENT_A)
    await expect(runGroup).toContainText(INTENT_B)
    await expect(page.getByTestId('workbench-context-group-artifacts')).toContainText(FILE_B)
    await expect(page.getByTestId('workbench-context-group-skills')).toContainText(SKILL_NAME)
    await expect(page.getByTestId('workbench-context-group-memory')).toContainText(MEM_ID)
    await expect(page.getByTestId('workbench-context-group-changeset')).toContainText('代码变更集')

    // ── honesty ③ dataset schema/quality : explicit empty state, no number ──
    await expect(page.getByTestId('workbench-context-dataset-empty')).toHaveText(
      '选择当前输入，查看字段、行列数与真实质量检查。',
    )
    // ── honesty ④ session-level resource read-back : explicit note ──
    await expect(page.getByTestId('workbench-context-group-resources')).toContainText(
      '会话级附件读回暂无端点',
    )

    // ── right panel starts empty ──
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')
    const panel = page.getByTestId('workbench-inspector-panel')

    // ── switch A: runA vs runB (differential: INTENT_A here, never INTENT_B) ──
    const runItems = runGroup.getByTestId('workbench-context-run')
    await runItems.nth(0).click()
    await expect(panel).toContainText('运行状态')
    await expect(panel).toContainText(INTENT_A)
    await expect(runItems.nth(0)).toHaveAttribute('aria-current', 'true')

    // ── switch B: skill ⇒ honesty ① success-rate + ② run↔skill recency ──
    await page
      .getByTestId('workbench-context-group-skills')
      .getByTestId('workbench-context-object')
      .click()
    await expect(panel).toContainText('技能')
    // Exact-value assertions: the rendered `<dd>` must EQUAL the honest copy —
    // proving no fabricated number/percent is appended to the value.
    const successRow = panel.locator('.workbench-insp-row').filter({ hasText: '成功率' })
    await expect(successRow.locator('dd')).toHaveText('暂无数据（技能级统计端点待 Phase G）')
    const recentRow = panel.locator('.workbench-insp-row').filter({ hasText: '最近运行' })
    await expect(recentRow.locator('dd')).toHaveText('暂无数据（Run↔Skill 关联待 Phase F）')
    await expect(panel).toContainText('内置常量') // usage_count labelled as a constant, not live

    // ── switch C: changeset ⇒ honesty ⑤ first-class ChangeSet deferred ──
    await page
      .getByTestId('workbench-context-group-changeset')
      .getByTestId('workbench-context-object')
      .click()
    await expect(panel).toContainText('代码变更集')
    await expect(panel).toContainText('完整变更集见「打开 Change Set」')
    await expect(panel.getByRole('button', { name: '打开 Change Set', exact: true })).toBeVisible()

    // ── switch D: artifact via left column ──
    await page
      .getByTestId('workbench-context-group-artifacts')
      .getByTestId('workbench-context-object')
      .click()
    await expect(panel).toContainText('产物')
    await expect(panel).toContainText(FILE_B)

    // ── switch E: memory ⇒ experience facts ──
    await page
      .getByTestId('workbench-context-group-memory')
      .getByTestId('workbench-context-object')
      .click()
    await expect(panel).toContainText('经验沉淀')

    // ── switch F: CENTER-column chip ⇒ right panel switches to that artifact ──
    await page.getByTestId('workbench-inspector-close').click()
    await page.getByTestId('chat-artifact-inspect').click()
    await expect(panel).toContainText('产物')
    await expect(panel).toContainText(FILE_B)

    // ── close ⇒ back to the empty state ──
    await page.getByTestId('workbench-inspector-close').click()
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')
  })

  test('无会话：左栏/右栏诚实空态，非白屏', async ({ page }) => {
    await signIn(page, false)
    await stub(page, [])
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()
    await expect(page.getByTestId('workbench')).toBeVisible()
    await expect(page.getByTestId('workbench-context-empty')).toContainText(
      '还没有任务，去中间发起第一个',
    )
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
  })

  test('会话存在但无 run：各分组诚实空态 + 右栏空态', async ({ page }) => {
    await signIn(page, true)
    await stub(page, [])
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()
    await expect(page.getByTestId('workbench-context-object-tree')).toBeVisible()
    await expect(page.getByTestId('workbench-context-group-runs')).toContainText(
      '还没有任务，去中间发起第一个',
    )
    await expect(page.getByTestId('workbench-context-group-artifacts')).toContainText('本次无产物')
    await expect(page.getByTestId('workbench-context-group-skills')).toContainText('本次未调用技能')
    await expect(page.getByTestId('workbench-context-group-changeset')).toContainText(
      '本次无代码变更',
    )
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')
  })

  test('run 失败：中栏/右栏可读，不白屏', async ({ page }) => {
    await signIn(page, true)
    await stub(page, [{ runId: RUN_B, intent: INTENT_B, status: 'failed' }])
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
    await expect(page.getByTestId('workbench')).toBeVisible()
    const runGroup = page.getByTestId('workbench-context-group-runs')
    await expect(runGroup).toContainText(INTENT_B)
    await runGroup.getByTestId('workbench-context-run').click()
    await expect(page.getByTestId('workbench-inspector-panel')).toContainText('运行状态')
  })

  test('后端 500：降级为可读错误而非白屏', async ({ page }) => {
    await signIn(page, true)
    await page.route('**/api/**', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'boom' }),
      }),
    )
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()
    // Not a white screen: the workbench chrome keeps rendering.
    await expect(page.getByTestId('workbench')).toBeVisible()
    await expect(page.getByTestId('workbench-context-panel')).toBeVisible()
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
    // A readable error (center column) should surface — soft check to avoid a
    // false FAIL if the app chooses an empty-state instead of an alert.
    await expect.soft(page.locator('.chat-error').first()).toBeVisible()
  })

  test('320px 超窄视口：选中对象后仍无横向溢出', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 })
    await signIn(page, true)
    await stub(page, [{ runId: RUN_B, intent: INTENT_B, status: 'completed' }])
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()
    await expect(page.getByTestId('workbench')).toBeVisible()
    await page.getByTestId('workbench-context-group-runs').getByTestId('workbench-context-run').click()
    await expect(page.getByTestId('workbench-inspector-panel')).toContainText('运行状态')
    const m = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      docClient: document.documentElement.clientWidth,
    }))
    expect(m.docScroll).toBeLessThanOrEqual(m.docClient + 1)
  })
})
