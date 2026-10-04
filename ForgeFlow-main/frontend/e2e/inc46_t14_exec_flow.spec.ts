/**
 * INC46 T14 —— 「执行流程图 + 交互收口」**真实浏览器** e2e。
 *
 * 范围：执行流程图（步骤 / 验证层 / HITL 暂停点节点 + 顺序 / 分支连线）、
 * 图层导航（L1/L2/L3）、交互三态（空 / 加载 / 错误）。
 *
 * 网络边界（沿用 inc43/inc34/inc33 spec 既有口径）：在 `/api/**` 边界 route 拦截
 * （stub 后端），专注验证**前端在给定后端响应下的真实交互**。stub 的响应体与真实
 * 端点契约逐字段对齐（`RunDetailResponse` / `PendingActionView`），**不新增后端 API**：
 *   · `GET /runs?limit=`     —— run 列表（真实端点）；
 *   · `GET /runs/{id}`       —— run 明细（真实 `steps` / `plan`）；
 *   · `GET /pending-actions` —— HITL 暂停点（T21 已挂载）。
 *
 * 三重探针（诚实纪律，§8 / 红线 12）：
 *   ① 阳性：有真实 run 数据 ⇒ 断言**节点数 + 连线数 + 类别分布**；
 *   ② 阴性：无 run 数据 ⇒ 显示「—」且 **0 节点 / 0 连线**（替代事实 + 阳性对照）；
 *   ③ 反事实：见报告——把「无数据 ⇒ 空态」分支注入假节点后，②必须转红。
 *
 * 纪律：data-testid **只增不改不删**；断言一律用会重试的 `toBeVisible` /
 * `toContainText` / `toHaveAttribute` / `toHaveCount` / `expect.poll`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 测试数据（真实 API 响应形状）
 * ------------------------------------------------------------------------- */

const TS = '2026-10-04T00:00:00Z'

const RUN_A = 'run_inc46_t14_a'
const RUN_B = 'run_inc46_t14_b'

function runSummary(runId: string, status: string) {
  return {
    run_id: runId,
    thread_id: `th-${runId}`,
    status,
    outcome: 'success',
    intent: '改写文档为正式语气',
    title: '文档改写任务',
    created_at: TS,
    completed_at: TS,
    experience_id: null,
  }
}

/** `GET /runs/{id}` —— RUN_A：3 个已执行步骤 + 1 个验证层判决 + 1 个未适用 + 1 个 HITL。 */
const DETAIL_A = {
  run_id: RUN_A,
  thread_id: `th-${RUN_A}`,
  status: 'awaiting_approval',
  outcome: 'success',
  intent: '改写文档为正式语气',
  errors: [],
  created_at: TS,
  completed_at: null,
  experience_id: null,
  steps: [
    { step_id: 's0', index: 0, tool: 'document.read', step_type: 'tool', status: 'ok', note: '读取原文' },
    {
      step_id: 's1',
      index: 1,
      tool: 'document.edit',
      step_type: 'tool',
      status: 'ok',
      note: '改写为正式语气',
      // 真实 `run_steps.verification` JSONB（migration 019 列）：验证层判决。
      verification: { layer: 'L2', passed: true, detail: '不变量未变' },
    },
    { step_id: 's2', index: 2, tool: 'artifact.save', step_type: 'tool', status: 'ok' },
  ],
  plan: {
    run_id: RUN_A,
    attempt: 1,
    steps: [
      { step_id: 's0', index: 0, tool: 'document.read', applicability: 'required' },
      { step_id: 's1', index: 1, tool: 'document.edit', applicability: 'required' },
      { step_id: 's2', index: 2, tool: 'artifact.save', applicability: 'required' },
    ],
    not_applicable: [{ tool: 'crm.export', reason: '无 CRM 输入' }],
  },
}

/** `GET /runs/{id}` —— RUN_B：仅 1 个步骤（用于验证切换运行会重新取数）。 */
const DETAIL_B = {
  run_id: RUN_B,
  thread_id: `th-${RUN_B}`,
  status: 'completed',
  outcome: 'success',
  intent: '只读检索',
  errors: [],
  created_at: TS,
  completed_at: TS,
  experience_id: null,
  steps: [{ step_id: 'b0', index: 0, tool: 'research.search', step_type: 'tool', status: 'ok' }],
  plan: { run_id: RUN_B, steps: [] },
}

/** `GET /pending-actions` —— RUN_A 的 HITL 暂停点（后端 `PendingActionView` 逐字段）。 */
const PENDING_A = [
  {
    pending_id: 'pa_inc46_t14_1',
    run_id: RUN_A,
    kind: 'approval',
    payload: { reason: '高风险改写' },
    status: 'waiting',
    expires_at: null,
    resolution: null,
    resolved_by: null,
    run_status: 'awaiting_approval',
    run_continues: false,
  },
]

/* ------------------------------------------------------------------------- *
 * 请求记录器
 * ------------------------------------------------------------------------- */
const rec = { runDetailGets: [] as string[], pendingGets: [] as string[] }

type ApiOpts = {
  runs?: unknown[]
  details?: Record<string, unknown>
  pending?: unknown[]
  runsStatus?: number
  runsDelayMs?: number
  skills?: unknown[]
}

async function stubApi(page: Page, opts: ApiOpts = {}) {
  const runs = opts.runs ?? [runSummary(RUN_A, 'awaiting_approval'), runSummary(RUN_B, 'completed')]
  const details = opts.details ?? { [RUN_A]: DETAIL_A, [RUN_B]: DETAIL_B }
  const pending = opts.pending ?? PENDING_A
  const skills = opts.skills ?? []

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    /* ---- 本次新增消费的真实端点 ---- */
    if (path === '/api/runs' && method === 'GET') {
      if (opts.runsDelayMs) await new Promise((r) => setTimeout(r, opts.runsDelayMs))
      if (opts.runsStatus && opts.runsStatus !== 200) {
        return route.fulfill({
          status: opts.runsStatus,
          contentType: 'application/json',
          body: JSON.stringify({ detail: '运行列表不可用' }),
        })
      }
      return json({ total: runs.length, items: runs })
    }
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      rec.runDetailGets.push(detailM[1])
      return json(details[detailM[1]] ?? {})
    }
    if (path === '/api/pending-actions' && method === 'GET') {
      rec.pendingGets.push(url.search)
      return json(pending)
    }

    /* ---- 技能资产中心数据源（页面其余部分） ---- */
    if (path === '/api/skills' && method === 'GET') {
      return json({ total: skills.length, items: skills })
    }
    if (/^\/api\/skills\/[^/]+\/versions$/.test(path) && method === 'GET') return json([])
    if (path === '/api/skill-candidates' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })

    /* ---- 应用外壳噪音（避免无关 4xx） ---- */
    if (path === '/api/model-status') return json({ connected: true, model: 'e2e', detail: '' })
    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    if (path === '/api/resources/limits') {
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    }
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    return json({})
  })
}

async function boot(page: Page, opts: ApiOpts = {}) {
  rec.runDetailGets = []
  rec.pendingGets = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, 'manager')
  await stubApi(page, opts)
  await page.goto('/skills')
}

/* ------------------------------------------------------------------------- *
 * ① 阳性 —— 有真实 run 数据 ⇒ 节点与连线渲染正确
 * ------------------------------------------------------------------------- */
test.describe('INC46 T14 —— 执行流程图', () => {
  test.use({ viewport: { width: 1440, height: 1000 } })

  test('① 有真实 run 数据 ⇒ 节点 6 + 连线 5（含验证层 + HITL + 分支）', async ({ page }) => {
    await boot(page)

    const section = page.getByTestId('exec-flow-section')
    await expect(section).toBeVisible()

    // 真发了明细与暂停点请求（含 run_id 过滤）。
    await expect.poll(() => rec.runDetailGets.includes(RUN_A)).toBe(true)
    await expect.poll(() => rec.pendingGets.some((q) => q.includes(`run_id=${RUN_A}`))).toBe(true)

    // 节点数 + 连线数（可复算）：3 步 + 1 验证层 + 1 未适用 + 1 HITL = 6 节点；
    // spine 顺序边 3 + 分支边 2 = 5 连线。
    await expect(page.getByTestId('exec-flow-node')).toHaveCount(6)
    await expect(page.getByTestId('exec-flow-edge')).toHaveCount(5)

    // 类别分布：step 4 / verify 1 / hitl 1。
    await expect(page.locator('[data-testid="exec-flow-node"][data-kind="verify"]')).toHaveCount(1)
    await expect(page.locator('[data-testid="exec-flow-node"][data-kind="hitl"]')).toHaveCount(1)
    await expect(page.locator('[data-testid="exec-flow-node"][data-kind="step"]')).toHaveCount(4)

    // 连线分布：seq 3 / branch 2（顺序 + 分支都能表达）。
    await expect(page.locator('[data-testid="exec-flow-edge"][data-kind="seq"]')).toHaveCount(3)
    await expect(page.locator('[data-testid="exec-flow-edge"][data-kind="branch"]')).toHaveCount(2)

    // 内容逐字来自 stub 的真实契约。
    await expect(page.getByTestId('exec-flow-node').first()).toContainText('document.read')
    await expect(page.locator('[data-testid="exec-flow-node"][data-kind="verify"]')).toContainText(
      '验证层 L2',
    )
    await expect(page.locator('[data-testid="exec-flow-node"][data-kind="hitl"]')).toContainText(
      'approval',
    )

    // 有数据时**不**得出现空态。
    await expect(page.getByTestId('exec-flow-empty')).toHaveCount(0)
    await expect(page.getByTestId('exec-flow-no-runs')).toHaveCount(0)
  })

  test('① 切换真实运行 ⇒ 重新取数并以该 run 数据重绘', async ({ page }) => {
    await boot(page)
    await expect(page.getByTestId('exec-flow-node')).toHaveCount(6)

    await page.getByTestId('exec-flow-run-select').selectOption(RUN_B)
    await expect.poll(() => rec.runDetailGets.includes(RUN_B)).toBe(true)

    await expect(page.getByTestId('exec-flow-node')).toHaveCount(1)
    await expect(page.getByTestId('exec-flow-edge')).toHaveCount(0)
    await expect(page.getByTestId('exec-flow-node').first()).toContainText('research.search')
  })

  /* ----------------------------------------------------------------------- *
   * ③ 反事实（阴性用例 —— 注入假节点即转红，见报告）
   * ----------------------------------------------------------------------- */

  test('② 无 run 数据 ⇒ 显示「—」且 0 节点 / 0 连线（不得伪造）', async ({ page }) => {
    await boot(page, { runs: [], pending: [] })

    // 替代事实：整段**确实**渲染了（不是「没挂载」），且真的发过列表请求。
    const section = page.getByTestId('exec-flow-section')
    await expect(section).toBeVisible()
    await expect(page.getByTestId('exec-flow-graph')).toHaveAttribute('data-phase', 'ready')

    // 无运行记录 + 无图数据 ⇒ 两处显式「—」。
    await expect(page.getByTestId('exec-flow-no-runs')).toBeVisible()
    await expect(page.getByTestId('exec-flow-no-runs')).toContainText('—')
    await expect(page.getByTestId('exec-flow-empty')).toBeVisible()
    await expect(page.getByTestId('exec-flow-empty')).toHaveText('—')

    // 阴性核心：**零**节点、**零**连线；且不得出现任何伪工具名。
    await expect(page.getByTestId('exec-flow-node')).toHaveCount(0)
    await expect(page.getByTestId('exec-flow-edge')).toHaveCount(0)
    await expect(section).not.toContainText('document.read')

    // 无数据时也不得有「运行选择器」（没有可选的运行）。
    await expect(page.getByTestId('exec-flow-run-select')).toHaveCount(0)
  })

  /* ----------------------------------------------------------------------- *
   * 交互收口：加载态 / 错误态
   * ----------------------------------------------------------------------- */

  test('加载态：run 列表未返回时显示 exec-flow-loading', async ({ page }) => {
    await boot(page, { runsDelayMs: 1500 })
    await expect(page.getByTestId('exec-flow-loading')).toBeVisible()
    await expect(page.getByTestId('exec-flow-loading')).toContainText('加载中')
    await expect(page.getByTestId('exec-flow-node')).toHaveCount(0)
  })

  test('错误态：run 列表 500 ⇒ 显示 exec-flow-error（role=alert）', async ({ page }) => {
    await boot(page, { runsStatus: 500 })
    await expect(page.getByTestId('exec-flow-error')).toBeVisible()
    await expect(page.getByTestId('exec-flow-error')).toHaveAttribute('role', 'alert')
    await expect(page.getByTestId('exec-flow-error')).toContainText('运行列表加载失败')
    await expect(page.getByTestId('exec-flow-node')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ④ 图层导航（L1 元数据 / L2 正文 / L3 资源）
 * ------------------------------------------------------------------------- */
test.describe('INC46 T14 —— 图层导航', () => {
  test('L1/L2/L3 三个图层 tab 齐备；未选中主体 ⇒ 诚实空态「—」', async ({ page }) => {
    await boot(page)

    await expect(page.getByTestId('skill-layer-nav')).toBeVisible()
    await expect(page.getByTestId('skill-layer-tab')).toHaveCount(3)
    await expect(page.locator('[data-testid="skill-layer-tab"][data-layer="L1"]')).toBeVisible()
    await expect(page.locator('[data-testid="skill-layer-tab"][data-layer="L2"]')).toBeVisible()
    await expect(page.locator('[data-testid="skill-layer-tab"][data-layer="L3"]')).toBeVisible()

    // 默认 L1 激活；无主体 ⇒ 面板显式「—」（不伪造字段）。
    await expect(page.getByTestId('skill-layer-nav')).toHaveAttribute('data-active', 'L1')
    await expect(page.getByTestId('skill-layer-panel')).toHaveAttribute('data-layer', 'L1')
    await expect(page.getByTestId('skill-layer-empty')).toHaveText('—')

    // 切到 L3：面板 data-layer 随动（交互生效）。
    await page.locator('[data-testid="skill-layer-tab"][data-layer="L3"]').click()
    await expect(page.getByTestId('skill-layer-nav')).toHaveAttribute('data-active', 'L3')
    await expect(page.getByTestId('skill-layer-panel')).toHaveAttribute('data-layer', 'L3')
  })
})
