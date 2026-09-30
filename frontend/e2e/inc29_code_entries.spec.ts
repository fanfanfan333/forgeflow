/**
 * INC29 / T03 —— 代码任务三入口控件（§11④）**真实浏览器** e2e。
 *
 * 目标：在**真实构建产物**（`vite preview`）上用真实浏览器驱动
 * `CodeTaskTimeline.tsx::CodeTaskTimeline` 新增的那一行三入口，机械判定「真入口」而非装饰：
 *
 *   1. 三入口存在且文案**逐字**：`code-entry-diff`=「查看差异」/
 *      `code-entry-tests`=「查看测试」/`code-entry-trace`=「查看轨迹」；既有定位
 *      `code-diff` / `code-tests` / `code-trace` 未受影响（回归钉子）。
 *   2. 「查看轨迹」**真展开**：点击前 `#code-trace`（原生 `<details>`）`open === false`；
 *      点击后 `open === true`（并且滚到它）。
 *   3. 「查看差异」「查看测试」**真滚动**：点前目标在视口外，点后目标进入视口，且
 *      `window.scrollY` 确实**增大**（证明是点击引发的真实下滚，而非「本来就在视口里」）。
 *
 * 网络边界（分层诚实）：本 spec 在 `/api/**` 边界做 route 拦截（stub 后端），专注验证
 * **前端**三入口的真实交互 —— 这正是 §11④ 的落点。后端语义（时间线 / Diff / 测试结论
 * 的真实产出）不在本 spec 范围。
 *
 * 引文纪律：一律 `文件名::符号名`（见 CodeTaskTimeline.tsx::CodeTaskTimeline）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const RUN_ID = 'run_inc29_code'

/**
 * 12 条时间线事件 —— 刻意够长：保证审批块（`#code-diff` / `#code-tests`）与 `#code-trace`
 * 初始都落在首屏**之下**，从而「点后进入视口」是真滚动的可证伪证据。
 */
const TIMELINE = Array.from({ length: 12 }, (_, i) => ({
  seq: i + 1,
  ts: `2026-01-01T00:00:${String(i).padStart(2, '0')}Z`,
  phase: 'execute',
  kind: 'action',
  status: i === 0 ? 'running' : 'ok',
  tool: `tool_${i}`,
  label: `执行步骤 ${i + 1}`,
  detail: `原始轨迹 ${i + 1}`,
  latency_ms: 120 + i,
}))

const DIFF_TEXT = [
  'diff --git a/src/app.py b/src/app.py',
  'index 1111111..2222222 100644',
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,1 +1,2 @@',
  ' print("hi")',
  '+print("world")',
].join('\n')

const RUN_SUMMARY = {
  run_id: RUN_ID,
  thread_id: 'thread_inc29',
  status: 'completed',
  outcome: 'success',
  intent: 'INC29 T03 三入口 e2e',
  title: 'INC29 T03 三入口 e2e',
  created_at: '2026-01-01T00:00:00Z',
  completed_at: '2026-01-01T00:01:00Z',
  experience_id: null,
  step_count: TIMELINE.length,
}

const RUN_DETAIL = {
  run_id: RUN_ID,
  thread_id: 'thread_inc29',
  status: 'completed',
  outcome: 'success',
  intent: 'INC29 T03 三入口 e2e',
  steps: [],
  errors: [],
  created_at: '2026-01-01T00:00:00Z',
  completed_at: '2026-01-01T00:01:00Z',
  experience_id: null,
  runtime_mode: 'deterministic',
  artifacts: [],
  codeplane: {
    timeline: TIMELINE,
    diff: DIFF_TEXT,
    tests: {
      measured: true,
      verdict: 'passed',
      passed: 5,
      failed: 0,
      errors: 0,
      failed_cases: [],
      command: 'pytest -q',
    },
    approval: {
      status: 'approved',
      approval_id: 'ap_1',
      decided_by: 'tester',
      decided_at: '2026-01-01T00:00:30Z',
    },
    committed: true,
    summary: { files_changed: 1, test_command: 'pytest -q', passed: 5, failed: 0, repair_rounds: 0 },
    affected_steps: [],
    engine: { available: true, reason: '' },
    workspace: { workspace_id: 'ws_1', state: 'released' },
    injected: { skills: [], memory: [] },
  },
}

async function stubApi(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/runs' && req.method() === 'GET') {
      return json({ total: 1, items: [RUN_SUMMARY] })
    }
    if (path === `/api/runs/${RUN_ID}` && req.method() === 'GET') {
      return json(RUN_DETAIL)
    }
    // 其余（session / 其它面板）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

/** 目标元素在竖直方向是否落在视口内。 */
async function inViewport(page: Page, id: string): Promise<boolean> {
  return page.evaluate((elId) => {
    const el = document.getElementById(elId)
    if (!el) return false
    const r = el.getBoundingClientRect()
    return r.top >= -1 && r.top < window.innerHeight
  }, id)
}

const traceOpen = (page: Page) =>
  page.evaluate(() => (document.getElementById('code-trace') as HTMLDetailsElement).open)

test.describe('INC29 / T03 代码任务三入口', () => {
  test.beforeEach(async ({ page }) => {
    await stubApi(page)
    await page.goto('/runs')
    // 代码执行面就位（非代码任务不渲染代码区块）。
    await expect(page.getByTestId('code-plane')).toBeVisible()
    await expect(page.getByTestId('code-timeline')).toBeVisible()
  })

  test('三入口存在、文案逐字，且既有定位 testid 未受影响', async ({ page }) => {
    const diff = page.getByTestId('code-entry-diff')
    const tests = page.getByTestId('code-entry-tests')
    const trace = page.getByTestId('code-entry-trace')
    await expect(diff).toHaveCount(1)
    await expect(tests).toHaveCount(1)
    await expect(trace).toHaveCount(1)
    await expect(diff).toHaveText('查看差异')
    await expect(tests).toHaveText('查看测试')
    await expect(trace).toHaveText('查看轨迹')
    // 既有定位 testid 仍在（回归钉子）：三入口只**新增** `id`，不改 `data-testid`。
    await expect(page.getByTestId('code-diff')).toHaveCount(1)
    await expect(page.getByTestId('code-tests')).toHaveCount(1)
    await expect(page.getByTestId('code-trace')).toHaveCount(1)
  })

  test('「查看轨迹」真展开 #code-trace', async ({ page }) => {
    const btn = page.getByTestId('code-entry-trace')
    await btn.scrollIntoViewIfNeeded()
    // 点前：原生 details 默认折叠（无 open）。
    expect(await traceOpen(page)).toBe(false)
    await btn.click()
    // 点后：open 置真（真实行为，非装饰）。
    await expect.poll(() => traceOpen(page)).toBe(true)
    await expect.poll(() => inViewport(page, 'code-trace')).toBe(true)
  })

  test('「查看差异」真滚动到 #code-diff', async ({ page }) => {
    const btn = page.getByTestId('code-entry-diff')
    // 先把入口滚进视口（消除 Playwright 点击前自动滚动的干扰），再取基线。
    await btn.scrollIntoViewIfNeeded()
    await expect(btn).toBeInViewport()
    expect(await inViewport(page, 'code-diff')).toBe(false)
    const yBefore = await page.evaluate(() => window.scrollY)
    await btn.click()
    await expect.poll(() => inViewport(page, 'code-diff')).toBe(true)
    const yAfter = await page.evaluate(() => window.scrollY)
    // 确实发生了向下滚动（真滚动，而不是「本来就在视口里」）。
    expect(yAfter).toBeGreaterThan(yBefore)
  })

  test('「查看测试」真滚动到 #code-tests', async ({ page }) => {
    const btn = page.getByTestId('code-entry-tests')
    await btn.scrollIntoViewIfNeeded()
    await expect(btn).toBeInViewport()
    expect(await inViewport(page, 'code-tests')).toBe(false)
    const yBefore = await page.evaluate(() => window.scrollY)
    await btn.click()
    await expect.poll(() => inViewport(page, 'code-tests')).toBe(true)
    const yAfter = await page.evaluate(() => window.scrollY)
    expect(yAfter).toBeGreaterThan(yBefore)
  })
})
