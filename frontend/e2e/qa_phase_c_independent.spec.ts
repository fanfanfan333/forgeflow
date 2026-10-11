/**
 * QA (Edward/严过关) —— Phase C「Change Set 抽屉」**独立** e2e 复核（非工程师 spec 的复跑）。
 *
 * 目的（对应本轮复验清单 4 / 7）：
 *   ① 用**精确匹配**（toHaveText，非 contains）证明诚实空态逐字：修改原因「未记录修改原因」、
 *      风险等级「—」、语法/类型/构建「不可验证（无 validation 端点）」、审批「pending」、
 *      应用「未应用」；diff/test 来自真后端形状。
 *   ② 阴性对照：非代码任务 ⇒ 左栏「本次无代码变更」、无变更集对象、抽屉**入口不可达**。
 *   ③ 失败可读降级：approve 打桩 500 / reject 打桩 409 ⇒ 抽屉内出**可读错误**，
 *      标题/抽屉仍在（**不**白屏、**不**静默）。
 *   ④ 窄屏 320px：抽屉打开时**无横向溢出**。
 *
 * ⚠️ 本 spec 的 `/api/**` 全是桩（前端契约证明）；payload 形状取自真后端一次真实代码任务
 * （QA 自跑，见 qa_tmp/_a2_qa_phaseC_probe.json）。后端语义由 QA 的真后端探针独立证明。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const SESSION_ID = 'sess_qa_pc_1'
const RUN_ID = 'run_qa_pc_1'
const INTENT = '修复 mathlib.add 的失败用例'

/** 真后端 unified diff 逐字（QA 真跑 git diff 得到）。 */
const REAL_DIFF =
  'diff --git a/mathlib.py b/mathlib.py\n' +
  'index 12ee743..4693ad3 100644\n' +
  '--- a/mathlib.py\n' +
  '+++ b/mathlib.py\n' +
  '@@ -1,2 +1,2 @@\n' +
  ' def add(a, b):\n' +
  '-    return a - b\n' +
  '+    return a + b\n'

function codeRunBody(withInjected = true): Record<string, unknown> {
  const cp: Record<string, unknown> = {
    engine: { available: true, interpreter: 'python' },
    degraded: null,
    outcome: 'succeeded',
    workspace: { workspace_id: 'ws_qa_pc_1', state: 'active' },
    timeline: [],
    tests: { passed: 3, failed: 0, errors: 0, failed_cases: [], command: 'pytest -q', verdict: 'passed', measured: true },
    diff: REAL_DIFF,
    summary: { files_changed: 1, test_command: 'pytest -q', passed: 3, failed: 0, repair_rounds: 0 },
    committed: false,
    approval: { status: 'pending', approval_id: 'qa-appr-1', decided_by: null, decided_at: null },
  }
  // T-B06：`withInjected` 控制 codeplane 形状——true=后端给了注入；false=修复前形状（无该键）。
  if (withInjected) cp.injected = { skills: [{ id: 'skill_doc_rewrite', version: '2.1', name: '文档重写' }], memory: [] }
  return {
    run_id: RUN_ID,
    thread_id: 'thread-qa1',
    status: 'awaiting_approval',
    outcome: 'partial',
    intent: INTENT,
    steps: [
      { index: 0, tool: 'code.execute', note: '代码执行面', status: 'ok' },
      { index: 1, tool: 'code.commit', note: '人工审批后提交变更', status: 'awaiting_approval' },
    ],
    errors: [],
    created_at: '2026-01-01T00:00:00Z',
    completed_at: '2026-01-01T00:01:00Z',
    experience_id: null,
    runtime_mode: 'deterministic',
    tool_invocations: [
      { tool: 'code.execute', status: 'ok', executed: true, latency_ms: 1666.6, step_id: `${RUN_ID}:0:1` },
    ],
    codeplane: cp,
    artifacts: [],
    session_id: SESSION_ID,
    parent_run_id: null,
  }
}

function nonCodeRunBody(): Record<string, unknown> {
  return {
    ...codeRunBody(),
    codeplane: {},
    status: 'completed',
    steps: [{ index: 0, tool: 'report.render', note: '生成报告', status: 'ok' }],
  }
}

function sessionDetailBody(): Record<string, unknown> {
  return {
    session_id: SESSION_ID,
    title: INTENT,
    runs: [
      { run_id: RUN_ID, thread_id: 'thread-qa1', status: 'awaiting_approval', outcome: 'partial',
        intent: INTENT, title: INTENT, created_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:01:00Z', experience_id: null, session_id: SESSION_ID, parent_run_id: null },
    ],
  }
}

type FailMode = { approve?: number; reject?: number }
async function stub(page: Page, codeTask = true, fail: FailMode = {}, withInjected = true) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    if (/^\/api\/codeplane\/runs\/[^/]+\/approve$/.test(path) && method === 'POST') {
      if (fail.approve) return json({ detail: 'Internal Server Error' }, fail.approve)
      return json({ run_id: 'run_resume_qa', thread_id: 'thread-qa1', status: 'approved', detail: {} })
    }
    if (/^\/api\/codeplane\/runs\/[^/]+\/reject$/.test(path) && method === 'POST') {
      if (fail.reject) return json({ detail: 'No pending code approval for this run' }, fail.reject)
      return json({ run_id: RUN_ID, thread_id: 'thread-qa1', status: 'rejected', detail: {} })
    }
    if (/^\/api\/runs\/[^/]+\/replan$/.test(path) && method === 'POST') {
      return json({ run_id: 'run_replan_qa', thread_id: 'thread-qa1', status: 'completed', detail: {} })
    }
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream',
        body: 'data: {"run_id":"' + RUN_ID + '","type":"run.completed","data":{"status":"completed"},"seq":1,"ts":"2026-01-01T00:00:00Z"}\n\ndata: [DONE]\n\n' })
    }
    if (/^\/api\/workspace\/sessions\/[^/]+$/.test(path) && method === 'GET') return json(sessionDetailBody())
    if (/^\/api\/runs\/[^/]+$/.test(path) && method === 'GET') return json(codeTask ? codeRunBody(withInjected) : nonCodeRunBody())
    if ((path === '/api/runs' || path === '/api/workspace/sessions') && method === 'GET') return json({ total: 0, items: [] })
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/skills') return json({ total: 0, items: [] })
    return json({ total: 0, items: [] })
  })
}

async function signIn(page: Page) {
  await page.addInitScript(
    ({ sessionId }) => {
      window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
      window.sessionStorage.setItem('forgeflow.role', 'admin')
      window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
      window.sessionStorage.setItem('forgeflow.conversation.e2e-qa', sessionId)
    },
    { sessionId: SESSION_ID },
  )
}

async function openDrawer(page: Page) {
  await page.getByRole('button', { name: '任务上下文与文件' }).click()
  await page.getByTestId('workbench-context-group-changeset').getByTestId('workbench-context-object').click()
  await page.getByTestId('workbench-inspector-open-changeset').click()
  await expect(page.getByTestId('change-set-drawer')).toBeVisible()
}

/** 精确读取某 facts 容器（默认整个抽屉）里某 label 行的 dd 值。 */
function factValue(page: Page, label: string, scope?: string) {
  const root = scope ? page.getByTestId(scope) : page.getByTestId('change-set-drawer')
  return root.locator('.change-set-fact', { hasText: label }).locator('dd')
}

test.describe('QA 独立复核 / Phase C Change Set 抽屉', () => {
  test('诚实空态逐字精确匹配（非 contains）：原因 / 风险 / type-build / 审批 / 应用', async ({ page }) => {
    await signIn(page)
    await stub(page)
    await page.goto('/')
    await openDrawer(page)

    // 头行：真实 diff 统计。
    await expect(page.getByTestId('change-set-title')).toHaveText(`Change Set #${RUN_ID}`)
    await expect(page.getByTestId('change-set-subtitle')).toHaveText('1 files changed · +1 / -1')

    // 修改原因：无真源 ⇒ 逐字「未记录修改原因」。
    await expect(page.getByTestId('change-set-reason')).toHaveText('未记录修改原因')

    // 风险等级：无真源 ⇒ 逐字「—」（精确）。
    await expect(factValue(page, '风险等级')).toHaveText('—')
    // HITL：pending 审批存在 ⇒ 「需要」。
    await expect(factValue(page, '是否需要 HITL')).toHaveText('需要')
    // 审批状态：逐字 pending。
    await expect(factValue(page, '审批状态')).toHaveText('pending')
    // 最终应用：committed=false ⇒ 「未应用」。
    await expect(factValue(page, '是否已应用')).toHaveText('未应用')

    // Validation：已跑校验（无 code.lint）＝「未跑校验工具」；type/build 恒「不可验证（无 validation 端点）」。
    await expect(factValue(page, '已跑校验', 'change-set-validation')).toHaveText('未跑校验工具')
    await expect(factValue(page, '语法 / 类型 / 构建', 'change-set-validation')).toHaveText('不可验证（无 validation 端点）')

    // 测试：真实评审证据（精确，限定在测试块内以免与其他「…命令…」事实串味）。
    await expect(factValue(page, '结论', 'change-set-test')).toHaveText('passed')
    await expect(factValue(page, '通过 / 失败', 'change-set-test')).toHaveText('3 / 0')
    await expect(factValue(page, '命令', 'change-set-test')).toHaveText('pytest -q')

    // diff 行：真实 + / - 各 1 行。
    await expect(page.locator('.change-set-diff-line.add')).toHaveCount(1)
    await expect(page.locator('.change-set-diff-line.rem')).toHaveCount(1)
  })

  test('阴性对照：非代码任务 ⇒ 无变更集对象、抽屉入口不可达', async ({ page }) => {
    await signIn(page)
    await stub(page, /* codeTask */ false)
    await page.goto('/')

    const group = page.getByTestId('workbench-context-group-changeset')
    await expect(group).toContainText('本次无代码变更')
    await expect(group.getByTestId('workbench-context-object')).toHaveCount(0)
    await expect(page.getByTestId('change-set-drawer')).toHaveCount(0)
  })

  test('失败可读降级：approve 500 ⇒ 抽屉内可读错误，不白屏', async ({ page }) => {
    await signIn(page)
    await stub(page, true, { approve: 500 })
    await page.goto('/')
    await openDrawer(page)

    await page.getByTestId('change-set-apply').click()
    const err = page.getByTestId('change-set-action-error')
    await expect(err).toBeVisible()
    await expect(err).toContainText('500')
    // 未白屏：抽屉与标题仍在。
    await expect(page.getByTestId('change-set-drawer')).toBeVisible()
    await expect(page.getByTestId('change-set-title')).toHaveText(`Change Set #${RUN_ID}`)
  })

  test('失败可读降级：reject 409 ⇒ 抽屉内可读错误（带原因）', async ({ page }) => {
    await signIn(page)
    await stub(page, true, { reject: 409 })
    await page.goto('/')
    await openDrawer(page)

    await page.getByTestId('change-set-reject').click()
    const err = page.getByTestId('change-set-action-error')
    await expect(err).toBeVisible()
    await expect(err).toContainText('409')
    await expect(page.getByTestId('change-set-drawer')).toBeVisible()
  })

  test('窄屏 320px：抽屉打开无横向溢出', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 })
    await signIn(page)
    await stub(page)
    await page.goto('/')
    await openDrawer(page)

    const m = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      docClient: document.documentElement.clientWidth,
      innerWidth: window.innerWidth,
    }))
    expect(m.docScroll).toBeLessThanOrEqual(m.docClient + 1)
    expect(m.docScroll).toBeLessThanOrEqual(m.innerWidth + 1)
  })

  /**
   * T-B06 独立复核（3b/3c）——左栏「本次涉及 · Skill」**随后端字段变化**，非硬编码：
   *   · 后端**不给** `codeplane.injected`（修复前形状）⇒ 诚实空态「本次未调用技能」；
   *   · 后端**给了** injected ⇒ 如实渲染出 skill 名。
   */
  test('T-B06：无 injected ⇒ 左栏 Skill 诚实空态；有 injected ⇒ 如实渲染（非硬编码）', async ({ page }) => {
    await signIn(page)
    await stub(page, true, {}, /* withInjected */ false)
    await page.goto('/')

    const skillsGroup = page.getByTestId('workbench-context-group-skills')
    await expect(skillsGroup).toContainText('本次未调用技能')
    await expect(skillsGroup).not.toContainText('文档重写')
    await expect(page.getByTestId('workbench-context-group-memory')).toContainText('本次未使用记忆')
  })

  test('T-B06 阳性对照：有 injected ⇒ 左栏 Skill 如实渲染（证明能力非恒定）', async ({ page }) => {
    await signIn(page)
    await stub(page, true, {}, /* withInjected */ true)
    await page.goto('/')

    await expect(page.getByTestId('workbench-context-group-skills')).toContainText('文档重写')
  })
})
