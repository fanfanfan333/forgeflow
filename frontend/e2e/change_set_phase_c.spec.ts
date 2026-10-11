/**
 * AgentFlow 2.0 / Phase C —— 「Change Set」抽屉端到端自证（T-C02）。
 *
 * 目标：在**真实浏览器**里证明 Phase C 的验收口径 —— 用户在**同一次代码任务**上下文里
 * 打开一等「变更集」抽屉，看到 **真实形状** 的字段，并让三个动作**真的**打既有端点。
 *
 * ⚠️ 诚实声明（沿用 T-B06 口径）：本 spec 的 `/api/**` **全是桩**。抽屉所消费的
 * `GET /runs/{id}` 桩 payload 的**形状**逐字取自**真后端**一次真实代码任务运行
 * （`tests/integration/test_tc02_change_set_loop.py` 落盘于
 * `qa_tmp/_a2_pC_T2_backend_payload.json`：真 git 仓库真改 `mathlib.py` ⇒ 真 unified diff
 * `+    return a + b`）。本 spec 只证明**前端契约**：
 *   ① 给定该（真后端形状的）payload，抽屉**如实**渲染 diff / 文件 / 测试 / 修改原因 /
 *      validation 诚实空态；
 *   ② 三动作按钮**真的**发出既有端点的请求（approve / reject / replan）。
 * 它**不**证明、也**不**声称后端语义 —— 后端由上述**真链路**集成用例独立证明。
 *
 * 作为对照（不掩盖真相）：
 *   · 第三例：**非代码任务**（`codeplane` 空）⇒ 左栏「本次无代码变更」、**没有**变更集
 *     对象 ⇒ 抽屉入口不可达。证明前端**绝不**臆造一个不存在的变更集。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route, Request } from '@playwright/test'

const SESSION_ID = 'sess_phase_c_1'
const RUN_ID = 'run_phase_c_1'
const INTENT = '修复 mathlib.add 的失败用例'

/** 真后端 `codeplane.diff` 的形状（逐字）：真 git 仓库真改 `mathlib.py`。 */
const REAL_DIFF =
  'diff --git a/mathlib.py b/mathlib.py\n' +
  'index 12ee743..4693ad3 100644\n' +
  '--- a/mathlib.py\n' +
  '+++ b/mathlib.py\n' +
  '@@ -1,2 +1,2 @@\n' +
  ' def add(a, b):\n' +
  '-    return a - b\n' +
  '+    return a + b\n'

/** 运行详情（`GET /runs/{id}`）—— 代码任务，停在 `awaiting_approval`（HITL）。 */
function codeRunBody(): Record<string, unknown> {
  return {
    run_id: RUN_ID,
    thread_id: 'thread-c1',
    status: 'awaiting_approval',
    outcome: 'partial',
    intent: INTENT,
    steps: [
      { index: 0, tool: 'code.execute', note: '代码执行面：在隔离工作区执行代码任务', status: 'ok' },
      { index: 1, tool: 'code.commit', note: '代码执行面：人工审批后提交变更', status: 'awaiting_approval' },
    ],
    errors: [],
    created_at: '2026-01-01T00:00:00Z',
    completed_at: '2026-01-01T00:01:00Z',
    experience_id: null,
    runtime_mode: 'deterministic',
    tool_invocations: [
      { tool: 'code.execute', status: 'ok', executed: true, latency_ms: 1600.2, step_id: `${RUN_ID}:0:0` },
    ],
    // 真后端 codeplane 形状（真 diff / 真 tests / pending approval / committed=false）。
    codeplane: {
      engine: { available: true, interpreter: 'python' },
      degraded: null,
      outcome: 'succeeded',
      workspace: { workspace_id: 'ws_phase_c_1', state: 'active' },
      timeline: [],
      tests: {
        passed: 3,
        failed: 0,
        errors: 0,
        failed_cases: [],
        command: 'pytest -q',
        verdict: 'passed',
        measured: true,
      },
      diff: REAL_DIFF,
      summary: { files_changed: 1, test_command: 'pytest -q', passed: 3, failed: 0, repair_rounds: 0 },
      injected: { skills: [], memory: [] },
      committed: false,
      approval: { status: 'pending', approval_id: 'appr-phase-c-1', decided_by: null, decided_at: null },
    },
    artifacts: [],
    session_id: SESSION_ID,
    parent_run_id: null,
  }
}

/** 非代码任务（`codeplane` 空）—— 阴性对照。 */
function nonCodeRunBody(): Record<string, unknown> {
  const body = codeRunBody()
  return { ...body, codeplane: {}, status: 'completed', steps: [{ index: 0, tool: 'report.render', note: '生成报告', status: 'ok' }] }
}

/** 会话详情（`GET /workspace/sessions/{id}`）—— 一条代码 run。 */
function sessionDetailBody(): Record<string, unknown> {
  return {
    session_id: SESSION_ID,
    title: INTENT,
    runs: [
      {
        run_id: RUN_ID,
        thread_id: 'thread-c1',
        status: 'awaiting_approval',
        outcome: 'partial',
        intent: INTENT,
        title: INTENT,
        created_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:01:00Z',
        experience_id: null,
        session_id: SESSION_ID,
        parent_run_id: null,
      },
    ],
  }
}

async function stub(page: Page, codeTask = true) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    // ── 三动作的**真端点**（断言其真被调用）─────────────────────────────────────
    if (/^\/api\/codeplane\/runs\/[^/]+\/approve$/.test(path) && method === 'POST') {
      return json({ run_id: 'run_resume_1', thread_id: 'thread-c1', status: 'approved', detail: {} })
    }
    if (/^\/api\/codeplane\/runs\/[^/]+\/reject$/.test(path) && method === 'POST') {
      return json({ run_id: RUN_ID, thread_id: 'thread-c1', status: 'rejected', detail: {} })
    }
    if (/^\/api\/runs\/[^/]+\/replan$/.test(path) && method === 'POST') {
      return json({ run_id: 'run_replan_1', thread_id: 'thread-c1', status: 'completed', detail: {} })
    }

    // 聊天事件流（让中栏收敛；与 Phase B 同法）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'data: {"run_id":"' + RUN_ID + '","type":"run.completed","data":{"status":"completed"},"seq":1,"ts":"2026-01-01T00:00:00Z"}\n\ndata: [DONE]\n\n',
      })
    }

    const sessionM = /^\/api\/workspace\/sessions\/([^/]+)$/.exec(path)
    if (sessionM && method === 'GET') return json(sessionDetailBody())

    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') return json(codeTask ? codeRunBody() : nonCodeRunBody())

    if ((path === '/api/runs' || path === '/api/workspace/sessions') && method === 'GET') {
      return json({ total: 0, items: [] })
    }
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

/** 左栏选中「代码变更集」⇒ 右栏出「打开 Change Set」按钮 ⇒ 打开抽屉。 */
async function openDrawer(page: Page) {
  await page.getByRole('button', { name: '任务上下文与文件' }).click()
  await page.getByTestId('workbench-context-group-changeset').getByTestId('workbench-context-object').click()
  await page.getByTestId('workbench-inspector-open-changeset').click()
  await expect(page.getByTestId('change-set-drawer')).toBeVisible()
}

const postTo = (re: RegExp) => (r: Request) =>
  r.method() === 'POST' && re.test(new URL(r.url()).pathname)

test.describe('AgentFlow 2.0 / Phase C Change Set 抽屉', () => {
  test('抽屉如实渲染真后端形状：标题 / 行数 / 文件 / diff / 修改原因 / validation / 测试', async ({ page }) => {
    await signIn(page)
    await stub(page)
    await page.goto('/')

    await openDrawer(page)

    // 标题 + 头行（数字来自**真实** diff 统计：1 文件 · +1 / -1）。
    await expect(page.getByTestId('change-set-title')).toHaveText(`Change Set #${RUN_ID}`)
    await expect(page.getByTestId('change-set-subtitle')).toHaveText('1 files changed · +1 / -1')

    // 文件列表（真实 diff 逐文件）。
    const files = page.getByTestId('change-set-file')
    await expect(files).toHaveCount(1)
    await expect(files.first()).toContainText('mathlib.py')
    await expect(files.first()).toContainText('+1 / -1')

    // unified diff 逐行：含真实改动行（+ / - 两行）。
    const diff = page.getByTestId('change-set-diff')
    await expect(diff).toContainText('@@ -1,2 +1,2 @@')
    await expect(diff).toContainText('+    return a + b')
    await expect(diff).toContainText('-    return a - b')
    await expect(page.locator('.change-set-diff-line.add')).toHaveCount(1)
    await expect(page.locator('.change-set-diff-line.rem')).toHaveCount(1)

    // 修改原因：无真源 ⇒ 诚实「未记录修改原因」（绝不臆造）。
    await expect(page.getByTestId('change-set-reason')).toHaveText('未记录修改原因')

    // Validation：只记真跑过的工具（未跑 ⇒ 诚实）；type/build 无端点 ⇒ 不可验证。
    const validation = page.getByTestId('change-set-validation')
    await expect(validation).toContainText('未跑校验工具')
    await expect(validation).toContainText('不可验证（无 validation 端点）')

    // 测试：真实评审证据（passed / 3 / 0 / pytest -q）。
    const testBlock = page.getByTestId('change-set-test')
    await expect(testBlock).toContainText('passed')
    await expect(testBlock).toContainText('3 / 0')
    await expect(testBlock).toContainText('pytest -q')

    // 三个动作按钮都在。
    await expect(page.getByTestId('change-set-apply')).toBeVisible()
    await expect(page.getByTestId('change-set-reject')).toBeVisible()
    await expect(page.getByTestId('change-set-reanalyze')).toBeVisible()

    // Esc 关闭 ⇒ 抽屉消失。
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('change-set-drawer')).toHaveCount(0)
  })

  test('三动作真的打既有端点：approve / reject / replan', async ({ page }) => {
    await signIn(page)
    await stub(page)
    await page.goto('/')
    await openDrawer(page)

    // [应用修改] → POST /api/codeplane/runs/{id}/approve
    const approveReq = page.waitForRequest(postTo(/^\/api\/codeplane\/runs\/[^/]+\/approve$/))
    await page.getByTestId('change-set-apply').click()
    await approveReq

    // [拒绝] → POST /api/codeplane/runs/{id}/reject
    const rejectReq = page.waitForRequest(postTo(/^\/api\/codeplane\/runs\/[^/]+\/reject$/))
    await page.getByTestId('change-set-reject').click()
    await rejectReq

    // [继续修改] → POST /api/runs/{id}/replan
    const replanReq = page.waitForRequest(postTo(/^\/api\/runs\/[^/]+\/replan$/))
    await page.getByTestId('change-set-reanalyze').click()
    await replanReq
  })

  /**
   * 阴性对照 —— **不掩盖真相**：非代码任务（`codeplane` 空）时，左栏「本次涉及 · 变更集」
   * 必须诚实空、「没有」变更集对象 ⇒ 抽屉入口不可达（前端**绝不**臆造一个不存在的变更集）。
   */
  test('阴性对照：非代码任务 ⇒ 无变更集对象，抽屉入口不可达', async ({ page }) => {
    await signIn(page)
    await stub(page, /* codeTask */ false)
    await page.goto('/')

    const group = page.getByTestId('workbench-context-group-changeset')
    await expect(group).toContainText('本次无代码变更')
    await expect(group.getByTestId('workbench-context-object')).toHaveCount(0)
    await expect(page.getByTestId('change-set-drawer')).toHaveCount(0)
  })
})
