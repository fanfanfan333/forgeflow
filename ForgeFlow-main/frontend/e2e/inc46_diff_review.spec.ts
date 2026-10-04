/**
 * INC46 T22 —— 「文档 Diff 预览与人工确认」**真实浏览器** e2e。
 *
 * 范围（后端 `forgeflow/api/routers/artifact_review.py` 的真实契约）：
 *   · diff 预览：段落 / run / 表格单元格三级 + 五类计数；
 *   · 区间外变化三种事实分离：未测量 / 已测量且干净 / 阻断（红线 4）；
 *   · 人工确认（`approve`）→ `committed`；拒绝（`reject`）→ `rejected`（红线 11）；
 *   · 越界阻断默认不可确认，勾选覆盖后才由后端裁决（红线 3）。
 *
 * 网络边界（沿用 inc43 / inc46_t14 既有口径）：在 `/api/**` 边界 route 拦截（stub 后端），
 * 专注验证**前端在给定后端响应下的真实交互**。stub 的响应体与真实端点契约逐字段对齐
 * （`DiffPreview` / `DocumentDiff.to_dict()`），**不新增后端 API**。
 *
 * 三重探针（诚实纪律，§8 / 红线 12）：
 *   ① 阳性：有真实 diff ⇒ 断言计数 + 段落数 + run 数 + 单元格数 + 状态徽标；
 *   ② 阴性：无深链 ⇒ 空态「—」且 0 段落（替代事实 + 阳性对照）；
 *      未测量 ⇒ `docdiff-oob-unmeasured`，且**不得**出现「已测量」或阻断面板；
 *   ③ 反事实：见报告——越界时去掉 approve 的 disabled 守卫后，阻断用例必须转红。
 *
 * 纪律：data-testid **只增不改不删**；断言一律用会重试的 `toBeVisible` /
 * `toContainText` / `toHaveAttribute` / `toHaveCount` / `expect.poll`；
 * 禁止 `toMatch`（Playwright locator 断言无此方法）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 测试数据 —— 与后端 `DiffPreview` / `DocumentDiff.to_dict()` 逐字段对齐
 * ------------------------------------------------------------------------- */
const ART = 'art_t22_demo'
const VERSION = 1

const DIFF = {
  format: 'docx',
  paragraphs: [
    {
      kind: 'modified',
      old_index: 3,
      new_index: 3,
      old_text: '本协议由双方签署。',
      new_text: '本合同由甲乙双方共同签署。',
      runs: [
        {
          index: 0,
          kind: 'modified',
          old_text: '本协议',
          new_text: '本合同',
          old_format: { bold: false },
          new_format: { bold: false },
        },
      ],
    },
    {
      kind: 'added',
      old_index: null,
      new_index: 8,
      old_text: '',
      new_text: '补充条款：未尽事宜另行协商。',
      runs: [],
    },
    {
      kind: 'modified',
      old_index: 5,
      new_index: 6,
      old_text: '金额 100 元',
      new_text: '金额 120 元',
      runs: [],
    },
  ],
  tables: [
    {
      index: 0,
      kind: 'modified',
      old_rows: 2,
      old_cols: 2,
      new_rows: 2,
      new_cols: 2,
      cells: [{ row: 1, col: 1, kind: 'modified', old_text: '100', new_text: '120' }],
    },
  ],
  counts: { modified: 2, added: 1, removed: 0, numeric_changes: 1, table_cells: 1 },
}

/** 一条**区间外**变化（落在目标区间 [0,4) 之外）。 */
const OUT_OF_REGION = [
  {
    kind: 'modified',
    old_index: 11,
    new_index: 12,
    position: 11,
    old_text: '越界原文',
    new_text: '越界新文',
  },
]

type Fixture = {
  state?: string
  outOfRegion?: unknown[] | null
  tracked?: boolean
  diffStatus?: number
  diffDelayMs?: number
  approveStatus?: number
}

const rec = {
  diffGets: [] as string[],
  approves: [] as { override: boolean; reason: string | null }[],
  rejects: [] as { reason: string | null }[],
  contentGets: [] as string[],
  trackedGets: [] as string[],
}

let fixture: Fixture = {}

/** 与后端 `DiffPreview` 逐字段同形的响应体。 */
function preview(): Record<string, unknown> {
  const oob = fixture.outOfRegion === undefined ? [] : fixture.outOfRegion
  const blocked = Array.isArray(oob) && oob.length > 0
  const state = fixture.state ?? 'pending'
  return {
    artifact_id: ART,
    version: VERSION,
    state,
    base_version: 0,
    approval_id: 'appr_t22_demo',
    run_id: 'run_t22_demo',
    diff: DIFF,
    out_of_region: oob,
    blocked,
    can_approve: state === 'pending' && !blocked,
    tracked_available: fixture.tracked ?? false,
  }
}

async function stubApi(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const base = `/api/artifacts/${ART}/versions/${VERSION}`

    /* ---- 本次新增消费的真实端点 ---- */
    if (path === `${base}/diff` && method === 'GET') {
      rec.diffGets.push(path)
      if (fixture.diffDelayMs) await new Promise((r) => setTimeout(r, fixture.diffDelayMs))
      if (fixture.diffStatus && fixture.diffStatus !== 200) {
        return json({ detail: 'Diff 预览不可用' }, fixture.diffStatus)
      }
      return json(preview())
    }
    if (path === `${base}/approve` && method === 'POST') {
      const body = JSON.parse(req.postData() || '{}') as {
        actor?: string
        override?: boolean
        override_reason?: string
      }
      rec.approves.push({ override: !!body.override, reason: body.override_reason ?? null })
      const blocked = Array.isArray(fixture.outOfRegion) && fixture.outOfRegion.length > 0
      // 后端红线 3：越界且未 override ⇒ 409（前端**原样**呈现，绝不折算成成功）。
      if (blocked && !body.override) {
        return json({ detail: '存在区间外变化，需显式 override' }, 409)
      }
      if (fixture.approveStatus && fixture.approveStatus !== 200) {
        return json({ detail: '确认失败' }, fixture.approveStatus)
      }
      fixture.state = 'committed'
      return json(preview())
    }
    if (path === `${base}/reject` && method === 'POST') {
      const body = JSON.parse(req.postData() || '{}') as { actor?: string; reason?: string }
      rec.rejects.push({ reason: body.reason ?? null })
      fixture.state = 'rejected'
      return json(preview())
    }
    if (path === `${base}/content` && method === 'GET') {
      rec.contentGets.push(path)
      return route.fulfill({
        status: 200,
        contentType:
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        body: Buffer.from('PKe2e-docx-bytes'),
      })
    }
    if (path === `${base}/tracked` && method === 'GET') {
      rec.trackedGets.push(path)
      return route.fulfill({
        status: 200,
        contentType:
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        body: Buffer.from('PKe2e-tracked-bytes'),
      })
    }

    /* ---- 应用外壳噪音（避免无关 4xx 干扰判定） ---- */
    if (path === '/api/model-status') return json({ connected: true, model: 'e2e', detail: '' })
    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/runs' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/skills' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/skill-candidates' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources/limits') {
      return json({ max_bytes: 1048576, supported_extensions: ['.docx'], note: '' })
    }
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    return json({})
  })
}

async function boot(page: Page, opts: Fixture = {}) {
  fixture = { outOfRegion: [], ...opts }
  rec.diffGets = []
  rec.approves = []
  rec.rejects = []
  rec.contentGets = []
  rec.trackedGets = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, 'manager')
  await stubApi(page)
}

const DEEP_LINK = `/artifacts/${ART}/${VERSION}`

/* ------------------------------------------------------------------------- *
 * ① 阳性 —— 有真实 diff ⇒ 段落 / run / 单元格 / 计数全部渲染
 * ------------------------------------------------------------------------- */
test.describe('INC46 T22 —— 文档 Diff 预览', () => {
  test.use({ viewport: { width: 1440, height: 1000 } })

  test('① 有真实 diff ⇒ 3 段落 / 1 run / 1 单元格 + 五类计数逐字', async ({ page }) => {
    await boot(page)
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-page')).toBeVisible()
    await expect(page.getByTestId('docdiff-target')).toContainText(`${ART}@v${VERSION}`)
    // 真的发过 diff 请求（不是「渲染了假数据」）。
    await expect.poll(() => rec.diffGets.length).toBeGreaterThan(0)

    // 状态徽标 + 审批单（后端原值）。
    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'pending')
    await expect(page.getByTestId('docdiff-state')).toHaveText('pending')
    await expect(page.getByTestId('docdiff-meta')).toContainText('appr_t22_demo')

    // 五类计数：与 stub 的 `counts` 逐键对齐。
    await expect(page.getByTestId('docdiff-count')).toHaveCount(5)
    await expect(page.locator('[data-testid="docdiff-count"][data-kind="modified"]')).toContainText('2')
    await expect(page.locator('[data-testid="docdiff-count"][data-kind="added"]')).toContainText('1')
    await expect(page.locator('[data-testid="docdiff-count"][data-kind="removed"]')).toContainText('0')
    await expect(
      page.locator('[data-testid="docdiff-count"][data-kind="numeric_changes"]'),
    ).toContainText('1')
    await expect(
      page.locator('[data-testid="docdiff-count"][data-kind="table_cells"]'),
    ).toContainText('1')

    // 段落 / run / 表格单元格。
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(3)
    await expect(page.locator('[data-testid="docdiff-paragraph"][data-kind="added"]')).toHaveCount(1)
    await expect(page.locator('[data-testid="docdiff-paragraph"][data-kind="modified"]')).toHaveCount(2)
    await expect(page.getByTestId('docdiff-run')).toHaveCount(1)
    await expect(page.getByTestId('docdiff-table')).toHaveCount(1)
    await expect(page.getByTestId('docdiff-cell')).toHaveCount(1)

    // 内容逐字来自 stub（不翻译、不臆造）。
    await expect(page.getByTestId('docdiff-paragraph').first()).toContainText('本协议由双方签署。')
    await expect(page.getByTestId('docdiff-paragraph').first()).toContainText('本合同由甲乙双方共同签署。')
    await expect(page.getByTestId('docdiff-run')).toContainText('本协议')
    await expect(page.getByTestId('docdiff-cell')).toContainText('100')
    await expect(page.getByTestId('docdiff-cell')).toContainText('120')

    // 已测量且无越界 ⇒ 显示「已测量」态，**不**是阻断、**不**是未测量。
    await expect(page.getByTestId('docdiff-oob-clean')).toBeVisible()
    await expect(page.getByTestId('docdiff-blocked')).toHaveCount(0)
    await expect(page.getByTestId('docdiff-oob-unmeasured')).toHaveCount(0)

    // pending + 未阻断 ⇒ 确认按钮可用。
    await expect(page.getByTestId('docdiff-approve-btn')).toBeEnabled()
  })

  test('① 人工确认 ⇒ POST approve 且状态转为 committed（红线 11）', async ({ page }) => {
    await boot(page)
    await page.goto(DEEP_LINK)
    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'pending')

    await page.getByTestId('docdiff-approve-btn').click()

    await expect.poll(() => rec.approves.length).toBe(1)
    // 未越界 ⇒ 后端未收到 override（前端不得**擅自**帮用户覆盖）。
    expect(rec.approves[0].override).toBe(false)
    await expect(page.getByTestId('docdiff-action-result')).toBeVisible()
    await expect(page.getByTestId('docdiff-action-result')).toContainText('committed')
    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'committed')
    // 已 committed ⇒ 不得再确认（按钮禁用）。
    await expect(page.getByTestId('docdiff-approve-btn')).toBeDisabled()
    await expect(page.getByTestId('docdiff-action-error')).toHaveCount(0)
  })

  test('① 拒绝 ⇒ POST reject 且状态转为 rejected', async ({ page }) => {
    await boot(page)
    await page.goto(DEEP_LINK)

    await page.getByTestId('docdiff-reject-reason').fill('语气不符合要求')
    await page.getByTestId('docdiff-reject-btn').click()

    await expect.poll(() => rec.rejects.length).toBe(1)
    expect(rec.rejects[0].reason).toBe('语气不符合要求')
    await expect(page.getByTestId('docdiff-action-result')).toContainText('rejected')
    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'rejected')
  })

  /* ----------------------------------------------------------------------- *
   * ② 区间外变化的三种事实（互斥，红线 4）
   * ----------------------------------------------------------------------- */

  test('② 越界非空 ⇒ 阻断面板 + 确认按钮默认禁用；勾选覆盖后才可提交', async ({ page }) => {
    await boot(page, { outOfRegion: OUT_OF_REGION })
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-blocked')).toBeVisible()
    await expect(page.getByTestId('docdiff-blocked')).toContainText('区间外变化')
    await expect(page.getByTestId('docdiff-oob-row')).toHaveCount(1)
    await expect(page.getByTestId('docdiff-oob-row')).toHaveAttribute('data-position', '11')
    await expect(page.getByTestId('docdiff-oob-row')).toContainText('越界新文')
    await expect(page.getByTestId('docdiff-oob-clean')).toHaveCount(0)
    await expect(page.getByTestId('docdiff-oob-unmeasured')).toHaveCount(0)

    // 核心守卫：默认**不可**确认（红线 3 / 11）。
    await expect(page.getByTestId('docdiff-approve-btn')).toBeDisabled()

    // 阳性对照：勾选「覆盖越界阻断」后按钮转为可用（守卫真的由勾选控制）。
    await page.getByTestId('docdiff-override-check').check()
    await expect(page.getByTestId('docdiff-approve-btn')).toBeEnabled()

    await page.getByTestId('docdiff-override-reason').fill('已人工复核越界段落')
    await page.getByTestId('docdiff-approve-btn').click()

    await expect.poll(() => rec.approves.length).toBe(1)
    expect(rec.approves[0].override).toBe(true)
    expect(rec.approves[0].reason).toBe('已人工复核越界段落')
    await expect(page.getByTestId('docdiff-action-result')).toContainText('committed')
  })

  test('③ 反事实：后端 409 ⇒ 如实呈现错误，绝不折算成成功', async ({ page }) => {
    // 阳性对照（同一文件上一个用例）已证明 200 ⇒ `docdiff-action-result`。
    // 这里把 approve 固定为 409，证明**同一按钮**在失败下不会伪造成功。
    await boot(page, { approveStatus: 409 })
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'pending')
    await expect(page.getByTestId('docdiff-approve-btn')).toBeEnabled()
    await page.getByTestId('docdiff-approve-btn').click()

    await expect.poll(() => rec.approves.length).toBe(1)
    await expect(page.getByTestId('docdiff-action-error')).toBeVisible()
    await expect(page.getByTestId('docdiff-action-error')).toContainText('409')
    // 失败**不得**写成成功：状态仍是 pending，成功文案一个都不出现。
    await expect(page.getByTestId('docdiff-action-result')).toHaveCount(0)
    await expect(page.getByTestId('docdiff-state')).toHaveAttribute('data-state', 'pending')
  })

  test('③ 反事实：越界且后端强制 409 ⇒ 即使勾了覆盖也只报错误', async ({ page }) => {
    await boot(page, { outOfRegion: OUT_OF_REGION, approveStatus: 409 })
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-blocked')).toBeVisible()
    await page.getByTestId('docdiff-override-check').check()
    await page.getByTestId('docdiff-approve-btn').click()

    await expect.poll(() => rec.approves.length).toBe(1)
    expect(rec.approves[0].override).toBe(true)
    await expect(page.getByTestId('docdiff-action-error')).toContainText('409')
    await expect(page.getByTestId('docdiff-action-result')).toHaveCount(0)
  })

  test('② 未测量（out_of_region=null）⇒ 显示「未测量」，不冒充「无越界」', async ({ page }) => {
    await boot(page, { outOfRegion: null })
    await page.goto(DEEP_LINK)

    // 替代事实：整段确实渲染了（不是「没挂载」）。
    await expect(page.getByTestId('docdiff-page')).toBeVisible()
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(3)

    await expect(page.getByTestId('docdiff-oob-unmeasured')).toBeVisible()
    await expect(page.getByTestId('docdiff-oob-unmeasured')).toContainText('未测量')
    // 三个事实互斥。
    await expect(page.getByTestId('docdiff-oob-clean')).toHaveCount(0)
    await expect(page.getByTestId('docdiff-blocked')).toHaveCount(0)
  })

  /* ----------------------------------------------------------------------- *
   * 三态收口：加载 / 错误 / 空
   * ----------------------------------------------------------------------- */

  test('加载态：diff 未返回 ⇒ docdiff-loading，且 0 段落', async ({ page }) => {
    await boot(page, { diffDelayMs: 1500 })
    await page.goto(DEEP_LINK)
    await expect(page.getByTestId('docdiff-loading')).toBeVisible()
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(0)
  })

  test('错误态：diff 500 ⇒ docdiff-error（role=alert），且 0 段落', async ({ page }) => {
    await boot(page, { diffStatus: 500 })
    await page.goto(DEEP_LINK)
    await expect(page.getByTestId('docdiff-error')).toBeVisible()
    await expect(page.getByTestId('docdiff-error')).toHaveAttribute('role', 'alert')
    await expect(page.getByTestId('docdiff-error')).toContainText('500')
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(0)
  })

  test('② 无深链 ⇒ 诚实空态「—」+ 选择入口，且 0 段落（不得伪造）', async ({ page }) => {
    await boot(page)
    await page.goto('/artifacts')

    await expect(page.getByTestId('docdiff-page')).toBeVisible()
    await expect(page.getByTestId('docdiff-picker')).toBeVisible()
    await expect(page.getByTestId('docdiff-empty')).toHaveText('—')
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(0)
    await expect(page.getByTestId('docdiff-counts')).toHaveCount(0)
    // 无目标 ⇒ 一个 diff 请求都**不该**发（避免注定 404 的请求）。
    expect(rec.diffGets).toHaveLength(0)
    await expect(page.getByTestId('docdiff-target')).toHaveCount(0)
  })

  test('选择入口：填制品 ID + 版本 ⇒ 跳转到深链并真的取数', async ({ page }) => {
    await boot(page)
    await page.goto('/artifacts')

    await page.getByTestId('docdiff-artifact-input').fill(ART)
    await page.getByTestId('docdiff-version-input').fill(String(VERSION))
    await page.getByTestId('docdiff-open-btn').click()

    await expect.poll(() => rec.diffGets.length).toBeGreaterThan(0)
    await expect(page.getByTestId('docdiff-target')).toContainText(`${ART}@v${VERSION}`)
    await expect(page.getByTestId('docdiff-paragraph')).toHaveCount(3)
  })

  /* ----------------------------------------------------------------------- *
   * 下载：修订模式 / 制品字节（真实端点）
   * ----------------------------------------------------------------------- */

  test('下载：制品字节走 .../content；无 base ⇒ 修订模式按钮禁用', async ({ page }) => {
    await boot(page, { tracked: false })
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-tracked-btn')).toBeDisabled()
    await expect(page.getByTestId('docdiff-content-btn')).toBeEnabled()

    await page.getByTestId('docdiff-content-btn').click()
    await expect.poll(() => rec.contentGets.length).toBeGreaterThan(0)
    expect(rec.contentGets[0]).toContain('/content')
    await expect(page.getByTestId('docdiff-download-error')).toHaveCount(0)
  })

  test('下载：有 base ⇒ 修订模式可用，点击走 .../tracked', async ({ page }) => {
    await boot(page, { tracked: true })
    await page.goto(DEEP_LINK)

    await expect(page.getByTestId('docdiff-tracked-btn')).toBeEnabled()
    await page.getByTestId('docdiff-tracked-btn').click()
    await expect.poll(() => rec.trackedGets.length).toBeGreaterThan(0)
    expect(rec.trackedGets[0]).toContain('/tracked')
  })
})
