/**
 * INC43 S2 —— 技能资产中心三栏（SkillLibrary | SkillEngineering | SkillInspector）
 * **真实浏览器** e2e。
 *
 * 范围：P0-10（三栏同屏）/ P0-11（左栏分组 + 搜索）/ P0-12（中栏卡片 + 计数）/
 * P0-13（右栏基本信息/能力/工具/评估/动作）。
 *
 * 网络边界（沿用 inc29/inc32/inc33/inc34/inc43-chat spec 既有口径）：在 `/api/**`
 * 边界 route 拦截（stub 后端），专注验证**前端在给定后端响应下的真实交互**——
 * 「真发请求」用捕获到的 method+path（含 query）钉住；「诚实空态」用「—」断言钉住
 * （而非 0 兜底）。后端语义本身由 pytest/realstack 覆盖，不在本 spec 范围。
 *
 * 纪律：
 *   · data-testid **只增不改不删**（本 spec 只消费，不新增 testid）；
 *   · 断言一律用会重试的 `toBeVisible` / `toContainText` / `toHaveAttribute` /
 *     `expect.poll`，**禁用 `allTextContents`**；
 *   · 登录沿用 inc33/inc34 口径：sessionStorage 三键 mock 会话。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 测试数据（真实 API 响应形状）
 * ------------------------------------------------------------------------- */

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

const TS = '2026-10-02T00:00:00Z'

const S_PUBLISHED: Skill = {
  id: 'skill_inc43_pub',
  tenant_id: 'tenant_qa',
  name: 'INC43 文档改写',
  domain: 'general',
  owner: 'e2e-qa',
  description: '将文档改写为正式语气',
  current_version: '2.1.0',
  status: 'published',
  usage_count: 183,
  featured: false,
  tags: [],
  created_at: TS,
  updated_at: TS,
}

const S_TESTING: Skill = {
  id: 'skill_inc43_test',
  tenant_id: 'tenant_qa',
  name: 'INC43 测试中技能',
  domain: 'general',
  owner: 'someone-else',
  description: '处于评估阶段的技能',
  current_version: '0.3.0',
  status: 'evaluating',
  usage_count: 7,
  featured: false,
  tags: [],
  created_at: TS,
  updated_at: TS,
}

const S_DRAFT: Skill = {
  id: 'skill_inc43_draft',
  tenant_id: 'tenant_qa',
  name: 'INC43 草稿技能',
  domain: 'general',
  owner: 'e2e-qa',
  description: '',
  current_version: null,
  status: 'draft',
  usage_count: 0,
  featured: false,
  tags: [],
  created_at: TS,
  updated_at: TS,
}

const CANDIDATE = {
  id: 'cand_inc43_1',
  tenant_id: 'tenant_qa',
  name: 'INC43 候选技能',
  domain: 'general',
  experience_ids: ['exp_inc43_1'],
  draft_spec: {
    prompt: '整理本周线索',
    steps: ['收集', '归类'],
    tools: ['research.search'],
  },
  similarity_score: 0.8,
  status: 'draft',
  created_at: TS,
}

/** `skill_inc43_pub` 的版本：**声明了** steps/tools/capabilities/policies（真实契约）。 */
const V_PUBLISHED = {
  id: 'ver_inc43_pub_1',
  skill_id: S_PUBLISHED.id,
  semver: '2.1.0',
  spec: {
    goal: '将文档改写为正式语气',
    prompt: '把这段文字改写得正式一些',
    steps: ['Inspect', 'Plan', 'Edit', 'Verify', 'Artifact'],
    tools: ['document.read', 'document.edit', 'artifact.save'],
    capabilities: ['DOCX', '文件编辑'],
    policies: ['p1', 'p2', 'p3', 'p4'],
    // 无 tests ⇒ 计数诚实显示「—」（不得 0 兜底）。
  },
  changelog: '正式版',
  eval_score: 0.964,
  source_experience_ids: [],
  approved_by: 'manager-1',
  created_at: TS,
  release_state: 'released',
}

/** `skill_inc43_test` 的版本：**未声明** steps（⇒ 前端骨架，declared:false）。 */
const V_TESTING = {
  id: 'ver_inc43_test_1',
  skill_id: S_TESTING.id,
  semver: '0.3.0',
  spec: { prompt: '看情况处理' },
  changelog: '试验版',
  eval_score: null,
  source_experience_ids: [],
  approved_by: null,
  created_at: TS,
}

/* ------------------------------------------------------------------------- *
 * 请求记录器
 * ------------------------------------------------------------------------- */
const rec = {
  /** `GET /skills` 的真实调用（含 query）—— 钉住搜索会真发请求。 */
  skillGets: [] as string[],
}

type ApiOpts = {
  skills?: Skill[]
  candidates?: unknown[]
  versionsBySkill?: Record<string, unknown[]>
}

/* ------------------------------------------------------------------------- *
 * API stub（`/api/**` 边界拦截）
 * ------------------------------------------------------------------------- */
async function stubApi(page: Page, opts: ApiOpts = {}) {
  const skills = opts.skills ?? [S_PUBLISHED, S_TESTING, S_DRAFT]
  const candidates = opts.candidates ?? [CANDIDATE]
  const versionsBySkill = opts.versionsBySkill ?? {
    [S_PUBLISHED.id]: [V_PUBLISHED],
    [S_TESTING.id]: [V_TESTING],
    [S_DRAFT.id]: [],
  }

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    /* ---- 技能资产中心数据源 ---- */
    if (path === '/api/skills' && method === 'GET') {
      rec.skillGets.push(`${path}${url.search}`)
      return json({ total: skills.length, items: skills })
    }
    const versionsM = /^\/api\/skills\/([^/]+)\/versions$/.exec(path)
    if (versionsM && method === 'GET') {
      return json(versionsBySkill[versionsM[1]] ?? [])
    }
    if (path === '/api/skill-candidates' && method === 'GET') {
      return json({ total: candidates.length, items: candidates })
    }
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })

    /* ---- 应用外壳噪音（避免无关 4xx） ---- */
    if (path === '/api/model-status') return json({ connected: true, model: 'e2e', detail: '' })
    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    if (path === '/api/runs' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    return json({})
  })
}

async function boot(page: Page, opts: ApiOpts = {}) {
  rec.skillGets = []
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, 'manager')
  await stubApi(page, opts)
  await page.goto('/skills')
}

/** 选中左栏某个分组下的第一条技能条目。 */
async function selectFromGroup(page: Page, group: string) {
  const groupNode = page.locator(`[data-testid="skill-library-group"][data-group="${group}"]`)
  await expect(groupNode).toBeVisible()
  await groupNode.getByTestId('skill-library-item').first().click()
}

/* ------------------------------------------------------------------------- *
 * ① P0-10：三栏同屏
 * ------------------------------------------------------------------------- */
test.describe('INC43 S2 —— 三栏资产中心', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('P0-10 三栏根节点同时可见（1440 宽均在视口内）', async ({ page }) => {
    await boot(page)

    await expect(page.getByTestId('skill-workspace')).toBeVisible()
    await expect(page.getByTestId('skill-library')).toBeVisible()
    await expect(page.getByTestId('skill-engineering')).toBeVisible()
    await expect(page.getByTestId('skill-inspector')).toBeVisible()

    // 三栏**同时在视口内**：逐个量取几何，确保横向完整落在视口宽度内
    // （不依赖可选匹配器，直接读 boundingBox，口径可复算）。
    const width = page.viewportSize()?.width ?? 0
    for (const id of ['skill-library', 'skill-engineering', 'skill-inspector']) {
      const box = await page.getByTestId(id).boundingBox()
      expect(box, `${id} 应可见`).not.toBeNull()
      expect(box!.x, `${id} 左边界在视口内`).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width, `${id} 右边界在视口内`).toBeLessThanOrEqual(width + 1)
    }
  })

  /* ----------------------------------------------------------------------- *
   * ② P0-11：左栏分组 + 搜索
   * ----------------------------------------------------------------------- */
  test('P0-11 左栏分组 data-group ∈ {published,testing,candidate,draft} 且条目可见', async ({ page }) => {
    await boot(page)

    const groups = page.getByTestId('skill-library-group')
    await expect(groups.first()).toBeVisible()

    // 四个 data-group 取值齐备（真实数据驱动）。
    const values = await groups.evaluateAll((els) =>
      els.map((el) => el.getAttribute('data-group')).sort(),
    )
    expect(values).toEqual(['candidate', 'draft', 'published', 'testing'])
    // 每个 data-group 值都在允许域内。
    for (const v of values) {
      expect(['published', 'testing', 'candidate', 'draft']).toContain(v)
    }

    await expect(page.getByTestId('skill-library-item').first()).toBeVisible()
  })

  test('P0-11 搜索框真发 GET /skills?q=', async ({ page }) => {
    await boot(page)
    await expect(page.getByTestId('skill-library-item').first()).toBeVisible()

    await page.getByTestId('skill-library-search').fill('文档')
    await expect.poll(() => rec.skillGets.some((u) => u.includes('q='))).toBe(true)
  })

  /* ----------------------------------------------------------------------- *
   * ③ P0-12：中栏卡片 + 计数（含诚实「—」）
   * ----------------------------------------------------------------------- */
  test('P0-12 中栏卡片：名称 + vX.Y + 徽标 + Goal + 节点链 + 三计数', async ({ page }) => {
    await boot(page)
    await selectFromGroup(page, 'published')

    const header = page.getByTestId('skill-card-header')
    await expect(header).toContainText(S_PUBLISHED.name)
    await expect(header).toContainText('v2.1.0')

    await expect(page.getByTestId('skill-card-goal')).toContainText('将文档改写为正式语气')

    // 节点链来自真实 spec.steps（5 节点，declared）。
    await expect(page.getByTestId('skill-exec-flow')).toBeVisible()
    await expect(page.getByTestId('skill-exec-node')).toHaveCount(5)
    await expect(page.getByTestId('skill-exec-node').first()).toHaveText('Inspect')
    await expect(page.getByTestId('skill-exec-node').first()).toHaveAttribute('data-declared', 'true')

    // 三计数：Tools 真实 = 3；Policies 真实 = 4；Tests 缺失 ⇒「—」（非 0）。
    const counts = page.getByTestId('skill-card-counts')
    await expect(counts).toContainText('Tools 3 tools')
    await expect(counts).toContainText('Policies 4 rules')
    await expect(counts).toContainText('Tests — cases')
  })

  test('P0-12 未声明步骤 ⇒ 骨架五节点且标注「骨架」（不影响计数）', async ({ page }) => {
    await boot(page)
    await selectFromGroup(page, 'testing')

    await expect(page.getByTestId('skill-exec-node')).toHaveCount(5)
    await expect(page.getByTestId('skill-exec-node').first()).toHaveText('Inspect')
    await expect(page.getByTestId('skill-exec-node').first()).toHaveAttribute('data-declared', 'false')
    await expect(page.getByTestId('skill-exec-flow')).toContainText('骨架（技能未声明步骤）')

    // 骨架**不伪造计数**：该 spec 无 tools/policies/tests ⇒ 三项均「—」。
    const counts = page.getByTestId('skill-card-counts')
    await expect(counts).toContainText('Tools — tools')
    await expect(counts).toContainText('Policies — rules')
    await expect(counts).toContainText('Tests — cases')
  })

  /* ----------------------------------------------------------------------- *
   * ④ P0-13：右栏 Inspector
   * ----------------------------------------------------------------------- */
  test('P0-13 右栏：基本信息/能力/工具/评估/动作齐备，且无 runs 编造', async ({ page }) => {
    await boot(page)
    await selectFromGroup(page, 'published')

    await expect(page.getByTestId('skill-inspector-basic')).toContainText(S_PUBLISHED.name)
    await expect(page.getByTestId('skill-inspector-basic')).toContainText('e2e-qa') // Owner
    await expect(page.getByTestId('skill-inspector-basic')).toContainText('v2.1.0')

    await expect(page.getByTestId('skill-inspector-capabilities')).toContainText('DOCX')
    await expect(page.getByTestId('skill-inspector-capabilities')).toContainText('文件编辑')

    await expect(page.getByTestId('skill-inspector-tools')).toContainText('document.read')
    await expect(page.getByTestId('skill-inspector-tools')).toContainText('document.edit')

    const evalBlock = page.getByTestId('skill-inspector-eval')
    await expect(evalBlock).toContainText('评估得分 0.96')
    await expect(evalBlock).toContainText('使用次数 183')
    // 反证：右栏**不得**出现 runs —— 收紧至**任意大小写**。
    // 原 `not.toContainText('runs')` 为大小写**敏感**子串，`Runs`/`RUNS`/`runs.` 会漏网。
    // 注：Playwright 的 *locator* 断言没有 `toMatch`；大小写不敏感的子串匹配须用
    // `toContainText(RegExp)`（传 `/runs/i`）—— 这才是与 `toMatch(/runs/i)` 等价的正确写法。
    await expect(page.getByTestId('skill-inspector')).not.toContainText(/runs/i)
    // 也不得编造成功率百分比（真实评估为 0.96 分值，绝非百分比）。
    await expect(page.getByTestId('skill-inspector')).not.toContainText('%')

    await expect(page.getByTestId('skill-action-versions')).toBeVisible()
    await expect(page.getByTestId('skill-action-test')).toBeVisible()
    await expect(page.getByTestId('skill-action-publish')).toBeVisible()
  })

  test('P0-13 候选主体：评估/版本缺失时诚实显示「—」', async ({ page }) => {
    await boot(page)
    await selectFromGroup(page, 'candidate')

    await expect(page.getByTestId('skill-inspector-basic')).toContainText(CANDIDATE.name)
    // 候选无评估/使用次数 ⇒「—」（不得 0 兜底）。
    await expect(page.getByTestId('skill-inspector-eval')).toContainText('评估得分 —')
    await expect(page.getByTestId('skill-inspector-eval')).toContainText('使用次数 —')
    // 候选无版本 ⇒ 版本按钮禁用。
    await expect(page.getByTestId('skill-action-versions')).toBeDisabled()
  })
})
