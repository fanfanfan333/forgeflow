/**
 * INC34 —— 技能市场真实化 + 技能创建/版本/导出 + run→技能沉淀 **真实浏览器** e2e。
 *
 * 背景（本仓教训）：`MarketplaceView` 曾是一个静态「即将上线」壳，`SkillsView` 没有
 * 任何创建/版本/导出入口，run 结果区也没有「沉淀为技能」。本轮若只用单测覆盖，等于
 * 「新按钮从未被真实浏览器点过」。故本 spec 为 INC34 六项改动**新增**回归钉子。
 *
 * 网络边界（分层诚实，沿用 inc29/inc32/inc33/inc36 spec 的既有口径）：在 `/api/**`
 * 边界做 route 拦截（stub 后端），专注验证**前端在给定后端响应下的真实交互**——
 * 「真发请求」用捕获到的 method+path 钉住；「诚实错误」用后端返回的 403/400 detail
 * 是否**逐字**上屏钉住。后端语义本身由 pytest/realstack 覆盖，不在本 spec 范围。
 *
 * 纪律：
 *   · data-testid **只增不改不删**（本 spec 只消费，不新增 testid；扫描域 frontend/src 不受影响）；
 *   · 断言一律用会重试的 `toBeVisible` / `toHaveText` / `toContainText` / `expect.poll`，
 *     **禁用 `allTextContents`**（本仓 17% 假红前科）；
 *   · 登录沿用 inc33/inc36 口径：sessionStorage 三键 mock 会话（不打真后端、不耗登录限流）。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 测试数据
 * ------------------------------------------------------------------------- */

const SKILL_ID = 'skill_inc34_a1'
const SKILL_NAME = 'INC34 周报生成器'

type Listing = {
  id: string
  tenant_id: string | null
  skill_id: string
  version: string
  name: string
  domain: string
  description: string
  shared: boolean
  listed_by: string | null
  rating: number
  rating_count: number
  installs: number
  created_at: string
}

const LISTING_1: Listing = {
  id: 'listing_inc34_1',
  tenant_id: 'tenant_qa',
  skill_id: SKILL_ID,
  version: '1.0.0',
  name: 'INC34 市场技能甲',
  domain: '数据分析',
  description: '把季度数据整理成复盘要点',
  shared: false,
  listed_by: 'manager-1',
  rating: 4.5,
  rating_count: 2,
  installs: 3,
  created_at: '2026-10-01T00:00:00Z',
}

const LISTING_2: Listing = {
  ...LISTING_1,
  id: 'listing_inc34_2',
  skill_id: 'skill_inc34_b2',
  name: 'INC34 市场技能乙',
  domain: 'general',
  rating: 0,
  rating_count: 0,
  installs: 0,
}

type Skill = {
  id: string
  tenant_id: string | null
  name: string
  domain: string
  owner: string | null
  description: string
  current_version: string | null
  status: string
  usage_count: number
  featured: boolean
  tags: string[]
  created_at: string
  updated_at: string
}

const SKILL_A: Skill = {
  id: SKILL_ID,
  tenant_id: 'tenant_qa',
  name: SKILL_NAME,
  domain: 'general',
  owner: 'manager-1',
  description: '把一次运行的经验固化为周报',
  current_version: '1.0.0',
  status: 'published',
  usage_count: 5,
  featured: false,
  tags: [],
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

const RUN_ID = 'run_inc34_capture'
const INTENT = 'INC34 沉淀 e2e：整理本周线索'
const EXPERIENCE_ID = 'exp_inc34_1'

/** 运行详情（`GET /api/runs/{id}`）：`experience_id` 非空 ⇒ hasExperience=true。 */
const RUN_DETAIL = {
  run_id: RUN_ID,
  thread_id: `thread_${RUN_ID}`,
  status: 'completed',
  outcome: 'success',
  intent: INTENT,
  title: INTENT,
  created_at: '2026-10-01T00:00:00Z',
  completed_at: '2026-10-01T00:01:00Z',
  experience_id: EXPERIENCE_ID,
  step_count: 1,
  session_id: 'sess_inc34',
  parent_run_id: null,
  steps: [{ index: 0, status: 'ok', tool: 'research.search', note: '' }],
  errors: [],
  artifacts: [],
  runtime_mode: 'deterministic',
}

const DRAFT_CANDIDATE = {
  id: 'cand_inc34_1',
  tenant_id: 'tenant_qa',
  name: '线索整理技能（候选）',
  domain: 'general',
  experience_ids: [EXPERIENCE_ID],
  draft_spec: {
    prompt: '整理本周线索',
    steps: ['收集', '归类'],
    tools: ['research.search'],
    io_schema: { input: { intent: 'string' }, output: { summary: 'string' } },
  },
  similarity_score: 0.82,
  status: 'draft',
  created_at: '2026-10-01T00:02:00Z',
}

/** 后端 400 的中文原因（逐字）；前端必须原样上屏，不许吞成「参数错误」。 */
const IO_SCHEMA_400_DETAIL =
  "技能 I/O 结构（io_schema）不合法：io_schema.type 含未知类型 'objekt'（允许：object、array、string、number、integer、boolean、null）"

/** 后端 403 的具体原因（逐字）。 */
const PUBLISH_403_DETAIL = 'DLP 预检未通过：检测到敏感内容，已拦截上架'

/* ------------------------------------------------------------------------- *
 * 请求记录器
 * ------------------------------------------------------------------------- */
const rec = {
  /** `GET /marketplace/skills` 的真实调用（含 query）—— 钉住「不再是静态壳」。 */
  marketplaceGets: [] as string[],
  /** 所有写动作（publish / install / rate / versions）。 */
  mutations: [] as { method: string; path: string; body: unknown }[],
  /** `GET /skills/{id}/export` 的真实调用。 */
  exportGets: [] as string[],
}

type ApiOpts = {
  listings?: Listing[]
  skills?: Skill[]
  /** publish 返回 403 时的 detail（设置即被拒）。 */
  publishForbidden?: string
  /** versions 返回 400 时的 detail（设置即被拒）。 */
  versionBadRequest?: string
}

/* ------------------------------------------------------------------------- *
 * API stub（`/api/**` 边界拦截）
 * ------------------------------------------------------------------------- */
async function stubApi(page: Page, opts: ApiOpts = {}) {
  const listings = opts.listings ?? [LISTING_1, LISTING_2]
  const skills = opts.skills ?? [SKILL_A]

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    /* ---- 技能市场 ---- */
    if (path === '/api/marketplace/skills' && method === 'GET') {
      rec.marketplaceGets.push(`${path}${url.search}`)
      return json({ total: listings.length, items: listings })
    }
    if (path === '/api/marketplace/skills/publish' && method === 'POST') {
      const body = req.postDataJSON()
      rec.mutations.push({ method, path, body })
      if (opts.publishForbidden) return json({ detail: opts.publishForbidden }, 403)
      return json({ published: true, listing: LISTING_1 })
    }
    const installM = /^\/api\/marketplace\/skills\/([^/]+)\/install$/.exec(path)
    if (installM && method === 'POST') {
      rec.mutations.push({ method, path, body: null })
      return json({ installed: true, listing: listings[0] ?? LISTING_1, installed_by: 'manager-1' })
    }
    const rateM = /^\/api\/marketplace\/skills\/([^/]+)\/rate$/.exec(path)
    if (rateM && method === 'POST') {
      rec.mutations.push({ method, path, body: req.postDataJSON() })
      return json({ rated: true, listing: listings[0] ?? LISTING_1 })
    }
    if (path === '/api/marketplace/templates' && method === 'GET') {
      return json({ total: 1, templates: [{ name: 'tpl_inc34', version: '1.0.0', description: '示例模板', domain: 'general' }] })
    }
    if (path === '/api/marketplace/templates/refresh' && method === 'POST') {
      rec.mutations.push({ method, path, body: null })
      return json({ refreshed: true, total: 1 })
    }

    /* ---- 技能中心 ---- */
    if (path === '/api/skills' && method === 'GET') {
      return json({ total: skills.length, items: skills })
    }
    const versionsM = /^\/api\/skills\/([^/]+)\/versions$/.exec(path)
    if (versionsM && method === 'GET') {
      return json([
        {
          id: 'ver_inc34_1',
          skill_id: versionsM[1],
          semver: '1.0.0',
          spec: { prompt: 'p', steps: ['s'], tools: ['t'], io_schema: { input: { a: 'string' } } },
          changelog: '初始版本',
          eval_score: null,
          source_experience_ids: [],
          approved_by: 'manager-1',
          created_at: '2026-10-01T00:00:00Z',
        },
      ])
    }
    if (versionsM && method === 'POST') {
      const body = req.postDataJSON()
      rec.mutations.push({ method, path, body })
      if (opts.versionBadRequest) return json({ detail: opts.versionBadRequest }, 400)
      return json({
        id: 'ver_inc34_2',
        skill_id: versionsM[1],
        semver: '1.0.1',
        spec: (body as { spec?: unknown })?.spec ?? {},
        changelog: '',
        eval_score: null,
        source_experience_ids: [],
        approved_by: 'manager-1',
        created_at: '2026-10-01T00:03:00Z',
      })
    }
    const exportM = /^\/api\/skills\/([^/]+)\/export$/.exec(path)
    if (exportM && method === 'GET') {
      rec.exportGets.push(exportM[1])
      return json({
        format: 'forgeflow.skill',
        format_version: 1,
        exported_at: '2026-10-01T00:04:00Z',
        skill: SKILL_A,
        version: null,
      })
    }

    /* ---- run → 技能沉淀 ---- */
    if (path === '/api/skill-candidates' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/skill-candidates' && method === 'POST') {
      const body = req.postDataJSON()
      rec.mutations.push({ method, path, body })
      return json(DRAFT_CANDIDATE)
    }
    if (/^\/api\/skill-candidates\/[^/]+\/evaluate$/.test(path) && method === 'POST') {
      rec.mutations.push({ method, path, body: null })
      return json({ id: 'ev_1', target_id: 'cand_inc34_1', dataset: null, metrics: {}, verdict: 'pass', created_at: '2026-10-01T00:05:00Z' })
    }
    if (/^\/api\/skill-candidates\/[^/]+\/promote$/.test(path) && method === 'POST') {
      rec.mutations.push({ method, path, body: null })
      return json({ id: 'ver_inc34_3', skill_id: SKILL_ID, semver: '1.0.0', spec: {}, changelog: '', eval_score: null, source_experience_ids: [], approved_by: 'manager-1', created_at: '2026-10-01T00:06:00Z' })
    }
    if (path === '/api/experiences' && method === 'GET') {
      return json({
        total: 1,
        items: [
          {
            id: EXPERIENCE_ID,
            tenant_id: 'tenant_qa',
            team_id: null,
            run_id: RUN_ID,
            summary: 'INC34 沉淀经验：整理本周线索',
            decisions: [],
            outcome: 'success',
            reusable_steps: [],
            tags: ['inc34'],
            memory_ids: [],
            created_at: '2026-10-01T00:02:00Z',
          },
        ],
      })
    }

    /* ---- 运行工作台基础数据 ---- */
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      if (detailM[1] !== RUN_ID) return json({ detail: 'not found' }, 404)
      return json(RUN_DETAIL)
    }
    if (path === '/api/runs' && method === 'GET') {
      const summary = { ...RUN_DETAIL } as Record<string, unknown>
      delete summary.steps
      delete summary.errors
      delete summary.artifacts
      delete summary.runtime_mode
      return json({ total: 1, items: [summary] })
    }
    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    // 其余（其它面板）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

async function boot(
  page: Page,
  opts: ApiOpts & { role?: string; path?: string } = {},
) {
  rec.marketplaceGets = []
  rec.mutations = []
  rec.exportGets = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, opts.role ?? 'manager')
  await stubApi(page, opts)
  await page.goto(opts.path ?? '/marketplace')
}

/* ------------------------------------------------------------------------- *
 * ① 市场真实化：不再是静态壳
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— 技能市场真实消费后端', () => {
  test('打开 /marketplace 真发 GET /marketplace/skills 并渲染列表项', async ({ page }) => {
    await boot(page, { role: 'manager', path: '/marketplace', listings: [LISTING_1, LISTING_2] })

    // 真发请求（不是静态壳）。
    await expect.poll(() => rec.marketplaceGets.length).toBeGreaterThan(0)
    expect(rec.marketplaceGets[0]).toContain('/api/marketplace/skills')

    // 列表容器渲染，且两条真实技能名逐字可见。
    await expect(page.getByTestId('marketplace-listings')).toBeVisible()
    await expect(page.getByText(LISTING_1.name, { exact: false })).toBeVisible()
    await expect(page.getByText(LISTING_2.name, { exact: false })).toBeVisible()
  })

  test('空数据 ⇒ 诚实空态「暂无上架技能」，绝不出现「即将上线」', async ({ page }) => {
    await boot(page, { role: 'manager', path: '/marketplace', listings: [] })

    await expect.poll(() => rec.marketplaceGets.length).toBeGreaterThan(0)
    await expect(page.getByTestId('marketplace-empty')).toContainText('暂无上架技能')
    // 反证：旧静态壳的「即将上线」不得回归。
    await expect(page.getByText('即将上线')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ② 发布 / 安装 / 评分：各触发对应 POST；失败诚实
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— 市场写动作真调后端', () => {
  test('安装 / 评分各触发对应 POST（method+path 钉住）', async ({ page }) => {
    await boot(page, { role: 'manager', path: '/marketplace', listings: [LISTING_1] })
    await expect(page.getByTestId('marketplace-listings')).toBeVisible()

    await page.getByRole('button', { name: '安装', exact: true }).click()
    await expect.poll(() => rec.mutations.map((m) => `${m.method} ${m.path}`)).toContain(
      `POST /api/marketplace/skills/${LISTING_1.id}/install`,
    )
    await expect(page.getByText('已安装到当前工作区')).toBeVisible()

    await page.getByRole('button', { name: '提交评分', exact: true }).click()
    await expect.poll(() => rec.mutations.map((m) => `${m.method} ${m.path}`)).toContain(
      `POST /api/marketplace/skills/${LISTING_1.id}/rate`,
    )
    await expect(page.getByText('已记录评分')).toBeVisible()
  })

  test('上架触发 POST /marketplace/skills/publish', async ({ page }) => {
    await boot(page, { role: 'manager', path: '/marketplace', listings: [LISTING_1] })

    await page.getByTestId('marketplace-publish').locator('summary').click()
    await page.getByTestId('marketplace-publish').locator('select').selectOption(SKILL_ID)
    await page.getByTestId('marketplace-publish').getByRole('button', { name: '上架', exact: true }).click()

    await expect.poll(() => rec.mutations.map((m) => `${m.method} ${m.path}`)).toContain(
      'POST /api/marketplace/skills/publish',
    )
    const call = rec.mutations.find((m) => m.path === '/api/marketplace/skills/publish')
    expect((call?.body as { skill_id?: string })?.skill_id).toBe(SKILL_ID)
  })

  test('上架被拒（403）⇒ 逐字错误上屏，绝不假成功', async ({ page }) => {
    await boot(page, {
      role: 'manager',
      path: '/marketplace',
      listings: [LISTING_1],
      publishForbidden: PUBLISH_403_DETAIL,
    })

    await page.getByTestId('marketplace-publish').locator('summary').click()
    await page.getByTestId('marketplace-publish').locator('select').selectOption(SKILL_ID)
    await page.getByTestId('marketplace-publish').getByRole('button', { name: '上架', exact: true }).click()

    const err = page.getByTestId('marketplace-publish').getByRole('alert')
    await expect(err).toContainText('上架失败（403）')
    // 逐字原因经 detail 通道到达用户：`humanizeError` 用状态短语做 label（403 ⇒
    // 「无权限」），但**绝不丢弃**后端原文——完整 detail 挂在 `title` 上（悬停可见）。
    await expect(err).toHaveAttribute('title', /DLP 预检未通过/)
    // 反证：绝不出现成功文案。
    await expect(page.getByText('已上架「')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ③ 技能中心：io_schema 校验（前端预检 + 后端 400 中文）
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— SkillsView io_schema 校验', () => {
  async function openEditor(page: Page, opts: ApiOpts) {
    await boot(page, { role: 'manager', path: '/skills', ...opts })
    await page.getByRole('button', { name: new RegExp(SKILL_NAME) }).click()
    await page.getByTestId('skill-version-editor').locator('summary').click()
    await expect(page.getByTestId('skill-version-editor').locator('textarea.skill-io')).toBeVisible()
  }

  test('非法 JSON ⇒ 前端预检上屏且不发请求（不吞错）', async ({ page }) => {
    await openEditor(page, {})

    await page.getByTestId('skill-version-editor').locator('textarea.skill-io').fill("{ bad json")
    await page.getByTestId('skill-version-save').click()

    await expect(page.getByTestId('skill-version-editor').getByRole('alert')).toContainText(
      'IO 结构不是合法 JSON',
    )
    // 前端拦下 ⇒ 一个 wire 请求都不该发出。
    expect(rec.mutations.filter((m) => /\/versions$/.test(m.path)).length).toBe(0)
  })

  test('后端 400 ⇒ 中文原因逐字上屏', async ({ page }) => {
    await openEditor(page, { versionBadRequest: IO_SCHEMA_400_DETAIL })

    // 合法 JSON，但结构非法 ⇒ 前端放行，后端 400。
    await page.getByTestId('skill-version-editor').locator('textarea.skill-io').fill('{"type":"objekt"}')
    await page.getByTestId('skill-version-save').click()

    await expect.poll(() => rec.mutations.filter((m) => /\/versions$/.test(m.path)).length).toBe(1)
    const err = page.getByTestId('skill-version-editor').getByRole('alert')
    await expect(err).toContainText('创建版本失败（400）')
    await expect(err).toContainText('含未知类型')
  })
})

/* ------------------------------------------------------------------------- *
 * ④ 版本导出：真调 GET /skills/{id}/export
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— 技能导出', () => {
  test('点击「导出 JSON」触发 GET /skills/{id}/export 并给出诚实反馈', async ({ page }) => {
    await boot(page, { role: 'manager', path: '/skills', skills: [SKILL_A] })

    await page.getByRole('button', { name: new RegExp(SKILL_NAME) }).click()
    await page.getByRole('button', { name: '导出 JSON', exact: true }).click()

    await expect.poll(() => rec.exportGets).toContain(SKILL_ID)
    await expect(page.getByText('已导出技能 JSON')).toBeVisible()
  })
})

/* ------------------------------------------------------------------------- *
 * ⑤ run → 技能沉淀入口（manager+ 且有经验；默认收起；三动作可达）
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— run→技能沉淀入口', () => {
  test('manager+ 且有经验时可见、默认收起，展开后三动作可达', async ({ page }) => {
    await boot(page, { role: 'manager', path: `/tasks/${RUN_ID}` })

    const capture = page.getByTestId('result-capture-skill')
    await expect(capture).toBeVisible()
    // 默认收起：面板不在 DOM。
    await expect(page.getByTestId('result-capture-panel')).toHaveCount(0)

    // 展开。
    await capture.locator('button.res-capture-toggle').click()
    await expect(page.getByTestId('result-capture-panel')).toBeVisible()
    await expect(page.getByTestId('result-capture-compile')).toBeVisible()

    // 编译 ⇒ 生成候选 ⇒ evaluate / promote 两动作出现（三动作齐）。
    await page.getByTestId('result-capture-compile').click()
    await expect(page.getByTestId('result-capture-evaluate')).toBeVisible()
    await expect(page.getByTestId('result-capture-promote')).toBeVisible()
  })

  test('viewer（低权限）看不到 result-capture-skill 入口', async ({ page }) => {
    await boot(page, { role: 'viewer', path: `/tasks/${RUN_ID}` })
    // 先确认运行详情确实加载（有经验）——避免「因为页面没渲染而误绿」。
    await expect(page.getByTestId('result-capture-panel')).toHaveCount(0)
    await expect(page.getByTestId('result-capture-skill')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ⑥ 角色门控不回退：viewer 直访 /marketplace ⇒ 提示 + 落回 /tasks
 * ------------------------------------------------------------------------- */

test.describe('INC34 —— 市场路由门控不回退', () => {
  test('viewer 直访 /marketplace ⇒ role-gate-toast 且回到 /tasks', async ({ page }) => {
    await boot(page, { role: 'viewer', path: '/marketplace' })

    await expect(page.getByTestId('role-gate-toast')).toBeVisible()
    await expect(page.getByTestId('role-gate-toast')).toHaveText(
      '当前角色无权访问该页面，已回到任务页',
    )
    await expect(page).toHaveURL(/\/tasks$/)
    // 反证：市场视图没有被渲染。
    await expect(page.getByTestId('marketplace-view')).toHaveCount(0)
  })
})
