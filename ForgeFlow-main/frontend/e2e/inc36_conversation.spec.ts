/**
 * INC36 —— 会话工作台「ChatGPT 式分层」**真实浏览器** e2e（`frontend/e2e`）。
 *
 * 背景（本仓教训）：曾出现「旧套件全绿」被当成「新功能已被真实浏览器验证」，而**新增按钮
 * 从未被任何用例点过**。本轮新增的 L1/L2 分层若零覆盖，「21/22 绿」就又是一个空壳。故本
 * spec 专为 INC36 **新增**用例，机械判定「默认极简 + 按需展开 + 深链可达 + follow-up 真发请求」。
 *
 * 网络边界（分层诚实，沿用 inc29/inc32/inc26 spec 的既有口径）：在 `/api/**` 边界做 route
 * 拦截（stub 后端），专注验证**前端**在给定后端响应下的真实交互。后端语义不在本 spec 范围。
 *
 * 覆盖（8 项）：
 *   1. L1 默认可见：`conv-user-turn` + `conv-run-summary` + `#res-panel-result` 可见。
 *   2. 默认极简（反证）：`conv-exec-*` **count=0**；证据/轨迹/成本 tabpanel 均不可见。
 *   3. L2 按需展开：点「查看执行详情」后 `conv-exec-*` 出现且可见；后端没有的类别不渲染。
 *   4. 「查看来源」：默认无 `open`；无来源时是诚实空态，且**不出现**「0 个来源」。
 *   5. L3 深链可达：点「详细 Trace ›」后 `#res-panel-trace` 可见。
 *   6. 深链：`/tasks/<run_id>` 刷新后中列标题逐字 = 该 run 的 `intent`。
 *   7. 左列按天分组：今天 / 昨天 / 2 天前 ⇒ 「今天」「昨天」「更早」三个组标题。
 *   8. follow-up：底部 `conv-followup` 提交后真发 `POST /api/workspace/tasks` 且带 `parent_run_id`。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const RUN_ID = 'run_inc36_1'
const INTENT = 'INC36 会话分层 e2e：分析销售线索'
const FOLLOWUP_TEXT = '继续：把结论整理成一页摘要'

/** 产物正文（逐字）：含「最终答案」节的**散文行** ⇒ `result-headline` 可见。 */
const ARTIFACT_CONTENT = [
  '# 运行报告',
  '',
  '**意图**：分析销售线索',
  '',
  '## 最终答案',
  '',
  '本次分析已完成，线索质量整体良好。',
  '- 线索 A 有效',
  '- 线索 B 待跟进',
  '',
].join('\n')

type Artifact = {
  id: string
  kind: string
  title: string
  format: string
  content: string
  source: string
  result_ref: string
  created_at: string
}

const ARTIFACT: Artifact = {
  id: 'art_inc36',
  kind: 'report_markdown',
  title: '运行报告.md',
  format: 'markdown',
  content: ARTIFACT_CONTENT,
  source: 'report.render',
  result_ref: 'inc36ref',
  created_at: '2026-01-01T00:00:10Z',
}

/**
 * 默认运行详情：
 *   · `plan.steps` ⇒ `Planner` 类 present；
 *   · `tool_invocations[research.search]` ⇒ `Tool` 类 present；
 *   · **没有** `knowledge.search` / `skill.invoke` / `memory.recall` ⇒ 那三类 **absent**（不渲染）；
 *   · `payload` 不含 URL ⇒ 来源诚实空态（且无「0 个来源」伪造计数）。
 */
type RunLike = {
  run_id: string
  thread_id: string
  status: string
  outcome: string
  intent: string
  title: string
  created_at: string
  completed_at: string | null
  experience_id: string | null
  step_count: number
  session_id: string
  parent_run_id: string | null
  steps: { index: number; status: string; tool?: string; note?: string }[]
  errors: string[]
  artifacts: Artifact[]
  plan?: { steps: { index: number; tool: string; note: string }[] }
  tool_invocations?: { tool: string; status: string; executed: boolean; step_id: string; latency_ms: number; payload: unknown }[]
  runtime_mode?: string
}

function detail(over: Partial<RunLike> & { run_id: string }): RunLike {
  return {
    run_id: over.run_id,
    thread_id: over.thread_id ?? `thread_${over.run_id}`,
    status: over.status ?? 'completed',
    outcome: over.outcome ?? 'success',
    intent: over.intent ?? '任务',
    title: over.title ?? over.intent ?? '任务',
    created_at: over.created_at ?? '2026-01-01T00:00:00Z',
    completed_at: over.completed_at === undefined ? '2026-01-01T00:01:00Z' : over.completed_at,
    experience_id: over.experience_id ?? null,
    step_count: over.step_count ?? (over.steps?.length ?? 0),
    session_id: over.session_id ?? 'sess_inc36',
    parent_run_id: over.parent_run_id ?? null,
    steps: over.steps ?? [],
    errors: over.errors ?? [],
    artifacts: over.artifacts ?? [],
    plan: over.plan,
    tool_invocations: over.tool_invocations,
    runtime_mode: over.runtime_mode ?? 'deterministic',
  }
}

const DEFAULT_DETAIL: RunLike = detail({
  run_id: RUN_ID,
  intent: INTENT,
  steps: [{ index: 0, status: 'ok', tool: 'research.search', note: '' }],
  artifacts: [ARTIFACT],
  plan: { steps: [{ index: 0, tool: 'research.search', note: '研究助手检索资料' }] },
  tool_invocations: [
    {
      tool: 'research.search',
      status: 'ok',
      executed: true,
      step_id: `${RUN_ID}:1:0`,
      latency_ms: 12,
      payload: { count: 3 },
    },
  ],
})

/** `RunDetail` → `RunSummary`（列表项形状）。 */
function summaryOf(d: RunLike) {
  const { steps, errors, artifacts, plan, tool_invocations, runtime_mode, ...rest } = d
  return { ...rest, step_count: steps.length }
}

type TaskBody = { intent?: string; parent_run_id?: string; context?: Record<string, unknown> }
const rec: { workspaceTasks: { method: string; path: string; body: TaskBody }[] } = {
  workspaceTasks: [],
}

async function stubApi(page: Page, seeds: RunLike[]) {
  const db: { order: string[]; detail: Record<string, RunLike> } = {
    order: seeds.map((r) => r.run_id),
    detail: Object.fromEntries(seeds.map((r) => [r.run_id, r])),
  }

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 派发（follow-up）：捕获 method/path/body，并让新句柄可被选中。
    if (path === '/api/workspace/tasks' && method === 'POST') {
      const body = req.postDataJSON() as TaskBody
      rec.workspaceTasks.push({ method, path, body })
      const id = `run_new_${db.order.length + 1}`
      const created = detail({
        run_id: id,
        status: 'running',
        outcome: '',
        intent: body.intent ?? '',
        completed_at: null,
        parent_run_id: body.parent_run_id ?? null,
      })
      db.detail[id] = created
      db.order.unshift(id)
      return json({
        run_id: id,
        thread_id: created.thread_id,
        status: created.status,
        detail: {},
        session_id: created.session_id,
        parent_run_id: created.parent_run_id,
      })
    }

    // 运行事件流（SSE）：空流，让实时条立即收尾（本 spec 不验证实时流）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }

    // 运行详情。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      const d = db.detail[detailM[1]]
      if (!d) return json({ detail: 'not found' }, 404)
      return json(d)
    }

    // 运行列表（左列数据源）。
    if (path === '/api/runs' && method === 'GET') {
      return json({ total: db.order.length, items: db.order.map((id) => summaryOf(db.detail[id])) })
    }

    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    // 其余（session / 其它面板）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

async function boot(page: Page, opts: { runs?: RunLike[]; role?: string; path?: string } = {}) {
  rec.workspaceTasks = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, opts.role ?? 'admin')
  await stubApi(page, opts.runs ?? [DEFAULT_DETAIL])
  await page.goto(opts.path ?? '/tasks')
}

/** 目标 tabpanel 是否**可见**（`hidden` ⇒ 不可见）。 */
const panelVisible = (page: Page, id: string) =>
  page.evaluate((elId) => {
    const el = document.getElementById(elId)
    if (!el) return false
    return el.getBoundingClientRect().height > 0 && getComputedStyle(el).display !== 'none'
  }, id)

test.describe('INC36 会话分层', () => {
  // ① L1 默认可见。
  test('L1 默认可见：用户消息块 + AI 执行摘要 + 结果面板', async ({ page }) => {
    await boot(page)
    await expect(page.getByTestId('conv-user-turn')).toBeVisible()
    await expect(page.getByTestId('conv-user-turn')).toContainText(INTENT)
    await expect(page.getByTestId('conv-run-summary')).toBeVisible()
    await expect(page.getByTestId('conv-run-summary').getByTestId('workspace-live-step')).toHaveCount(1)
    // 自然语言结果（既有结果面板）默认可见。
    await expect(page.locator('#res-panel-result')).toBeVisible()
    await expect(page.getByTestId('result-headline')).toHaveText('本次分析已完成，线索质量整体良好。')
  })

  // ② 默认极简（反证）：结构化执行详情字段不在 DOM；证据/轨迹/成本 tabpanel 不可见。
  test('默认极简：conv-exec-* 不进 DOM，三个深层 tabpanel 均不可见', async ({ page }) => {
    await boot(page)
    await expect(page.locator('[data-testid^="conv-exec-"]')).toHaveCount(0)
    expect(await panelVisible(page, 'res-panel-evidence')).toBe(false)
    expect(await panelVisible(page, 'res-panel-trace')).toBe(false)
    expect(await panelVisible(page, 'res-panel-cost')).toBe(false)
  })

  // ③ L2 按需展开：点「查看执行详情」⇒ conv-exec-* 出现；后端没有的类别不渲染。
  test('L2 按需展开：conv-exec-* 出现且可见；后端缺失的类别不渲染', async ({ page }) => {
    await boot(page)
    await page.getByTestId('workspace-exec-detail').locator('summary').click()

    // present 的两类出现且可见。
    await expect(page.getByTestId('conv-exec-planner')).toBeVisible()
    await expect(page.getByTestId('conv-exec-tool')).toBeVisible()
    // absent 的三类**不渲染**（诚实：present=false 不出假行）。
    await expect(page.getByTestId('conv-exec-knowledge')).toHaveCount(0)
    await expect(page.getByTestId('conv-exec-skill')).toHaveCount(0)
    await expect(page.getByTestId('conv-exec-memory')).toHaveCount(0)
    // 四 Tab 导航条仍在（L3）。
    await expect(page.getByTestId('result-tab-trace')).toBeVisible()
  })

  // ④ 「查看来源」：默认折叠 + 诚实空态 + 无「0 个来源」。
  test('「查看来源」默认折叠，空来源为诚实文案且无伪造计数', async ({ page }) => {
    await boot(page)
    const sources = page.getByTestId('conv-sources')
    await expect(sources).toBeVisible()
    await expect(sources).toHaveJSProperty('open', false)
    await expect(sources).toContainText('本次运行未记录可展示的来源')
    // L2 折叠区里**不出现**「0 个来源」这类伪造计数（L3 证据 Tab 的真实计数不在此断言域内）。
    await expect(sources.getByText('0 个来源')).toHaveCount(0)
  })

  // ⑤ L3 深链可达：点「详细 Trace ›」⇒ #res-panel-trace 可见。
  test('「详细 Trace ›」点击后 #res-panel-trace 可见', async ({ page }) => {
    await boot(page)
    await page.getByTestId('workspace-exec-detail').locator('summary').click()
    expect(await panelVisible(page, 'res-panel-trace')).toBe(false)
    await page.getByTestId('conv-exec-trace').click()
    await expect(page.locator('#res-panel-trace')).toBeVisible()
  })

  // ⑥ 深链：/tasks/<run_id> 刷新后中列标题逐字 = 该 run 的 intent。
  test('深链 /tasks/<run_id> 恢复会话：中列标题逐字 = intent', async ({ page }) => {
    await boot(page, { path: `/tasks/${RUN_ID}` })
    await expect(page.locator('.runs-head h1')).toHaveText(INTENT)
    await expect(page.getByTestId('conv-user-turn')).toContainText(INTENT)
  })

  // ⑦ 左列按天分组：今天 / 昨天 / 更早。
  test('左列按天分组：今天 / 昨天 / 更早', async ({ page }) => {
    const now = new Date()
    const at = (dayOffset: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + dayOffset, 10, 0, 0).toISOString()
    const dayRuns = [
      detail({ run_id: 'run_today', intent: '今天的任务', created_at: at(0) }),
      detail({ run_id: 'run_yesterday', intent: '昨天的任务', created_at: at(-1) }),
      detail({ run_id: 'run_earlier', intent: '更早的任务', created_at: at(-2) }),
    ]
    await boot(page, { runs: dayRuns })
    // `toHaveText` 带自动重试，等列表异步加载完成后再逐字比对三个组标题。
    await expect(page.getByTestId('session-group-title')).toHaveText(['今天', '昨天', '更早'])
  })

  // ⑧ follow-up：真发 POST /api/workspace/tasks 且带 parent_run_id。
  test('follow-up 真发请求：POST /api/workspace/tasks 且 body 带 parent_run_id', async ({ page }) => {
    await boot(page)
    await page.locator('#conv-followup-input').fill(FOLLOWUP_TEXT)
    await page.getByTestId('conv-followup').getByRole('button').click()

    await expect.poll(() => rec.workspaceTasks.length).toBe(1)
    const call = rec.workspaceTasks[0]
    expect(call.method).toBe('POST')
    expect(call.path).toBe('/api/workspace/tasks')
    expect(call.body.intent).toBe(FOLLOWUP_TEXT)
    expect(call.body.parent_run_id).toBe(RUN_ID)
  })
})
