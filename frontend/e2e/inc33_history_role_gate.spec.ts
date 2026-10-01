/**
 * INC33 —— P0 历史持久化 + P1 角色门控 **真实浏览器** e2e（`frontend/e2e`）。
 *
 * 背景（本仓教训）：「工程师自述通过」不等于「行为被真实浏览器钉住」。本 spec 为 INC33
 * 两项改动**新增**回归钉子（只增不改不删既有 testid；新增断言一律用会重试的
 * `toHaveText` / `toBeVisible`，不用 `allTextContents`）：
 *
 * P0（左列历史两源合并，`views/runs/history.ts` + `LiveRunsView`）：
 *   持久底 `GET /workspace/sessions` + 易失补充 `GET /runs`，按 run_id 去重、持久项为准；
 *   持久行 `step_count` **缺席** ⇒ 绝不渲染「N 步」。
 *   1. sessions 非空 / runs 空 ⇒ 左列渲染持久会话行、该行**无「步」字样**、点击后选中
 *      （`aria-current`）且详情按 `session_id`（= 首 run 的 run_id）拉取。
 *   2. sessions 与 runs 均空 ⇒ `session-history-empty` 诚实空态「暂无历史任务」。
 *   3. 两源合并：易失运行中项补充置前且显示**真实**步数；持久行仍无「步」（一行两证）。
 *
 * P1（角色门控，`auth/roleGate.ts` + `Sidebar` + `router.tsx::guardedShellChild`）：
 *   4. viewer 直访 `/security` ⇒ `role-gate-toast` 一次性中文提示 + 重定向落回 `/tasks`。
 *   5. viewer 侧栏：仅用户区 5 项可见；无「更多」组、无管理员区任何项。
 *   6. manager 侧栏：有「更多」组（展开可达 manager 目的地）但**无**管理员区项。
 *
 * 网络边界：沿用 inc32/inc36 spec 的既有口径 —— 在 `/api/**` 边界 route 拦截（stub 后端），
 * 专注验证前端在给定后端响应下的真实交互。角色用 mock 会话（sessionStorage
 * `forgeflow.jwt` / `forgeflow.role`），不打真后端、不消耗登录限流。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 测试数据
 * ------------------------------------------------------------------------- */

/** 今天 10:00 的 ISO 串（按天分组归「今天」，`conversation.ts::groupRunsByDay`）。 */
function todayAt(hour: number): string {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, 0, 0).toISOString()
}

type SessionSeed = {
  session_id: string
  title: string
  created_at: string
  run_count: number
  latest_status: string
}

const SESS_A: SessionSeed = {
  session_id: 'run_sess_a01',
  title: 'INC33 持久会话甲：整理季度复盘',
  created_at: todayAt(9),
  run_count: 2,
  latest_status: 'completed',
}
const SESS_B: SessionSeed = {
  session_id: 'run_sess_b02',
  title: 'INC33 持久会话乙：分析销售线索',
  created_at: todayAt(10),
  run_count: 1,
  latest_status: 'completed',
}

type RunSeed = {
  run_id: string
  status: string
  intent: string
  title: string
  created_at: string
  step_count?: number
}

/** 易失运行中项（`GET /runs`）：未被持久快照覆盖 ⇒ 合并后**置前**。 */
const LIVE_RUN: RunSeed = {
  run_id: 'run_live_c03',
  status: 'running',
  intent: 'INC33 运行中任务：实时汇总',
  title: 'INC33 运行中任务：实时汇总',
  created_at: todayAt(11),
  step_count: 3,
}

/** 详情 mock（`GET /runs/{id}`）的最小真实形状 —— 字段全部来自种子，绝不臆造。 */
function detailOf(runId: string, intent: string) {
  return {
    run_id: runId,
    thread_id: `thread_${runId}`,
    status: 'completed',
    outcome: 'success',
    intent,
    title: intent,
    created_at: todayAt(9),
    completed_at: todayAt(9),
    experience_id: null,
    step_count: 0,
    session_id: runId,
    parent_run_id: null,
    steps: [],
    errors: [],
    artifacts: [],
  }
}

/** 记录详情请求，钉住「点击持久行 ⇒ 按 session_id 拉详情」。 */
const rec: { detailGets: string[] } = { detailGets: [] }

/* ------------------------------------------------------------------------- *
 * API stub（沿用 inc36 口径：`/api/**` 边界拦截）
 * ------------------------------------------------------------------------- */

async function stubApi(
  page: Page,
  opts: { sessions?: SessionSeed[]; runs?: RunSeed[] } = {},
) {
  const sessions = opts.sessions ?? []
  const runs = opts.runs ?? []
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 持久底：会话分组（INC33 P0 的列表主体）。
    if (path === '/api/workspace/sessions' && method === 'GET') {
      return json({ total: sessions.length, items: sessions })
    }
    // 易失补充：进程内运行列表。
    if (path === '/api/runs' && method === 'GET') {
      return json({
        total: runs.length,
        items: runs.map((r) => ({
          run_id: r.run_id,
          thread_id: `thread_${r.run_id}`,
          status: r.status,
          outcome: '',
          intent: r.intent,
          title: r.title,
          created_at: r.created_at,
          completed_at: null,
          experience_id: null,
          step_count: r.step_count,
          session_id: r.run_id,
        })),
      })
    }
    // 运行事件流（SSE）：空流，立即收尾（本 spec 不验证实时流）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    // 运行详情：记录请求路径（钉「按 session_id 拉取」）。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      rec.detailGets.push(detailM[1])
      const known = [SESS_A, SESS_B].find((s) => s.session_id === detailM[1])
      if (known) return json(detailOf(known.session_id, known.title))
      const live = runs.find((r) => r.run_id === detailM[1])
      if (live) return json({ ...detailOf(live.run_id, live.intent), status: live.status, completed_at: null })
      return json({ detail: 'not found' }, 404)
    }
    if (path === '/api/resources/limits') {
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    }
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })
    // 其余（其余面板 / 会话杂项）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

/** 既有 spec 同款登录 mock：sessionStorage 三键（不打真后端、不耗限流）。 */
async function boot(
  page: Page,
  opts: { role?: string; path?: string; sessions?: SessionSeed[]; runs?: RunSeed[] } = {},
) {
  rec.detailGets = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, opts.role ?? 'admin')
  await stubApi(page, opts)
  await page.goto(opts.path ?? '/tasks')
}

/* ------------------------------------------------------------------------- *
 * P0 —— 历史持久化（两源合并 + step_count 缺席不渲染）
 * ------------------------------------------------------------------------- */

test.describe('INC33 P0 —— 左列历史持久化', () => {
  // ① sessions 非空 ⇒ 渲染持久行；该行无「N 步」；点击选中 + 按 session_id 拉详情。
  test('持久会话行渲染、无「步」字样、点击后按 session_id 拉取详情并选中', async ({ page }) => {
    await boot(page, { sessions: [SESS_A, SESS_B] })

    const rowB = page.getByRole('button', { name: new RegExp(SESS_B.title) })
    await expect(rowB).toBeVisible()
    // 持久行 step_count 缺席 ⇒ 绝不渲染「N 步」（history.ts::sessionToRunSummary 诚实纪律）。
    // 徽标文案（已完成 / 进行中等）均不含「步」（realRun.ts::runStatusMeta），断言域安全。
    await expect(rowB).not.toContainText('步')

    await rowB.click()
    // 点击 ⇒ 选中（aria-current）且详情按 session_id（= 首 run 的 run_id）拉取。
    await expect(rowB).toHaveAttribute('aria-current', 'true')
    await expect.poll(() => rec.detailGets).toContain(SESS_B.session_id)
  })

  // ② 两源皆空 ⇒ 诚实空态（绝不伪造历史）。
  test('sessions 与 runs 均空 ⇒ session-history-empty 空态「暂无历史任务」', async ({ page }) => {
    await boot(page, { sessions: [], runs: [] })
    await expect(page.getByTestId('session-history-empty')).toHaveText('暂无历史任务')
    // 空态下绝不出现任何运行行。
    await expect(page.locator('.run-list .run-item')).toHaveCount(0)
  })

  // ③ 两源合并：易失运行中项补充（显示真实步数）；持久行仍无「步」。
  test('两源合并：运行中项显示真实步数，持久行无「步」且同列共存', async ({ page }) => {
    await boot(page, { sessions: [SESS_A], runs: [LIVE_RUN] })

    // 易失项（真实 step_count=3）⇒ 渲染真实「3 步」。
    const liveRow = page.getByRole('button', { name: new RegExp(LIVE_RUN.title) })
    await expect(liveRow).toBeVisible()
    await expect(liveRow).toContainText('3 步')
    // 持久行仍无「步」（合并不改变持久行的 step_count 缺席语义）。
    const sessRow = page.getByRole('button', { name: new RegExp(SESS_A.title) })
    await expect(sessRow).toBeVisible()
    await expect(sessRow).not.toContainText('步')
  })
})

/* ------------------------------------------------------------------------- *
 * P1 —— 角色门控（路由守卫 + Sidebar 可见性分层）
 * ------------------------------------------------------------------------- */

test.describe('INC33 P1 —— 角色门控', () => {
  // ④ viewer 直访 /security ⇒ 一次性中文提示 + 重定向 /tasks。
  test('viewer 直访 /security ⇒ role-gate-toast 提示且落回 /tasks', async ({ page }) => {
    await boot(page, { role: 'viewer', path: '/security' })
    const toast = page.getByTestId('role-gate-toast')
    await expect(toast).toBeVisible()
    await expect(toast).toHaveText('当前角色无权访问该页面，已回到任务页')
    await expect(page).toHaveURL(/\/tasks$/)
  })

  // ⑤ viewer 侧栏：仅用户区 5 项；无「更多」、无管理员区任何项。
  test('viewer 侧栏：仅用户区 5 项，无「更多」组与管理员区项', async ({ page }) => {
    await boot(page, { role: 'viewer' })
    const sidebar = page.locator('.sidebar')
    // 用户区 5 项全部可见（Sidebar.tsx::USER_NAV）。
    for (const label of ['工作区', '新任务', '最近任务', '技能', '知识库']) {
      await expect(sidebar.getByRole('link', { name: label, exact: true })).toBeVisible()
    }
    // 无「更多」组、无管理员区项（roleGate.ts::roleRank 未知/低权兜底 viewer）。
    await expect(sidebar.getByText('更多', { exact: true })).toHaveCount(0)
    for (const label of ['安全与权限', '审计', '系统设置', '角色权限']) {
      await expect(sidebar.getByRole('link', { name: label, exact: true })).toHaveCount(0)
    }
  })

  // ⑥ manager 侧栏：有「更多」组（展开可达 manager 目的地），无管理员区项。
  test('manager 侧栏：「更多」组可见且可展开，管理员区项不渲染', async ({ page }) => {
    await boot(page, { role: 'manager' })
    const sidebar = page.locator('.sidebar')
    // 「更多」折叠组对 manager 渲染；展开后 manager 目的地真实可达。
    await sidebar.getByText('更多', { exact: true }).click()
    await expect(sidebar.getByRole('link', { name: '智能体工作台', exact: true })).toBeVisible()
    // 管理员区项（含自「更多」迁入的「角色权限」）对 manager 一律不渲染。
    for (const label of ['安全与权限', '审计', '系统设置', '角色权限']) {
      await expect(sidebar.getByRole('link', { name: label, exact: true })).toHaveCount(0)
    }
  })
})
