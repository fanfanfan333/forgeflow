/**
 * INC43 S3 / T03 —— 技能工程闭环**真实浏览器** e2e（消除「已声明但不可达」的孤儿能力）。
 *
 * 范围：中栏 `SkillEngineering` 新增的 `skill-eng-*` 区段确实**调用了**后端闭环端点
 * （`GET /skills/{id}/engineering`、`GET /skills/{id}/lifecycle`、
 * `GET/POST /skill-candidates/{id}/engineering`），并如实渲染其响应：
 *   · 技能：`skill-eng-lifecycle`（六态 + `next_states` + `requires_approval` ⇒ /approvals 链接）、
 *     `skill-eng-critique`（severity + findings + must_fix）、`skill-eng-tests`（四类计数 + verdict）、
 *     `skill-eng-eval`（比率 / 样本）。
 *   · 候选：`skill-eng-run` 触发 `POST`；`sample_size===0` ⇒ 比率一律「—」（**绝不** `0%`）；
 *     `degraded_reason` 非空 ⇒ 逐字展示。
 *
 * 网络边界（沿用 inc32/inc43 spec 既有口径）：在 `/api/**` 边界 route 打桩，专注验证
 * **前端在给定后端响应下的真实交互**——「真发请求」用捕获到的 method+path 钉住；
 * 「诚实空态」用「—」/「无阻断项」断言钉住（而非 0 兜底）。后端语义本身不在本 spec 范围。
 *
 * 纪律：断言一律用会重试的 `toBeVisible` / `toContainText` / `toHaveCount` / `expect.poll`，
 * **禁用 `allTextContents`**（读属性用 `evaluateAll`，照 inc43 既有口径）。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const TS = '2026-10-02T00:00:00Z'

/* ------------------------------------------------------------------------- *
 * 夹具（真实 API 响应形状，逐字对齐 forgeflow/api/hub_schemas.py）
 * ------------------------------------------------------------------------- */

const S = {
  id: 'skill_inc43_eng',
  tenant_id: 'tenant_qa',
  name: 'INC43 工程技能',
  domain: 'general',
  owner: 'e2e-qa',
  description: '用于验证工程闭环接线',
  current_version: '1.0.0',
  status: 'published',
  usage_count: 1,
  featured: false,
  tags: [],
  created_at: TS,
  updated_at: TS,
}

const V = {
  id: 'ver_inc43_eng_1',
  skill_id: S.id,
  semver: '1.0.0',
  spec: { goal: '把文档改正式', steps: ['Inspect', 'Edit'], tools: ['document.edit'], capabilities: ['DOCX'] },
  changelog: '初版',
  eval_score: 0.9,
  source_experience_ids: [],
  approved_by: 'manager-1',
  created_at: TS,
}

const CONTRACT = {
  goal: '把文档改正式',
  preconditions: [],
  inputs: { doc: 'string' },
  outputs: { doc: 'string' },
  procedure: ['Inspect', 'Edit'],
  tools: ['document.edit'],
  policies: [],
  verification: [],
  applicable_when: {},
  not_applicable_when: {},
  risk_level: 'medium',
}

/** 技能主体的工程闭环事实：有 3 条用例、`requires_approval=true`。 */
const ENG_SKILL = {
  tenant_id: 'tenant_qa',
  candidate_id: '',
  skill_id: S.id,
  lifecycle: 'TESTING',
  contract: CONTRACT,
  critique: { findings: [{ message: '缺少边界用例' }], severity: 'low', must_fix: [] },
  test_cases: [
    { id: 'case_normal_1', category: 'normal', input: {}, expectation: 'ok', assertion: '' },
    { id: 'case_boundary_1', category: 'boundary', input: {}, expectation: 'ok', assertion: '' },
    { id: 'case_adversarial_1', category: 'adversarial', input: {}, expectation: 'ok', assertion: '' },
  ],
  test_runs: [
    { case_id: 'case_normal_1', verdict: 'pass', detail: '' },
    { case_id: 'case_boundary_1', verdict: 'fail', detail: '越界' },
    { case_id: 'case_adversarial_1', verdict: 'error', detail: '工具不可用' },
  ],
  evaluation: {
    pass_rate: 0.5,
    verified_pass_rate: 0.5,
    failure_modes: ['边界缺失'],
    sample_size: 3,
    ran_at: TS,
  },
  revisions: [],
  rounds: 1,
  passed: false,
  degraded_reason: '',
  next_states: ['REVIEW', 'DRAFT'],
  requires_approval: true,
}

const LC_SKILL = {
  skill_id: S.id,
  candidate_id: '',
  lifecycle: 'TESTING',
  states: ['DRAFT', 'CANDIDATE', 'TESTING', 'REVIEW', 'APPROVED', 'DEPRECATED'],
  next_states: ['REVIEW'],
  requires_approval: true,
}

const C = {
  id: 'cand_inc43_eng',
  tenant_id: 'tenant_qa',
  name: 'INC43 工程候选',
  domain: 'general',
  experience_ids: [],
  draft_spec: { prompt: '整理线索' },
  similarity_score: 0.5,
  status: 'evaluating',
  created_at: TS,
}

/** 候选主体的工程闭环事实：**无用例**（`sample_size===0`）、有阻断项、有降级原因。 */
const ENG_CANDIDATE = {
  tenant_id: 'tenant_qa',
  candidate_id: C.id,
  skill_id: '',
  lifecycle: 'DRAFT',
  contract: { ...CONTRACT, risk_level: 'high' },
  critique: { findings: [], severity: 'high', must_fix: ['补全 goal'] },
  test_cases: [],
  test_runs: [],
  evaluation: {
    pass_rate: 0,
    verified_pass_rate: 0,
    failure_modes: [],
    sample_size: 0,
    ran_at: '',
  },
  revisions: [],
  rounds: 0,
  passed: false,
  degraded_reason: '沙箱无可用用例',
  next_states: ['DRAFT'],
  requires_approval: false,
}

/* ------------------------------------------------------------------------- *
 * 请求记录器
 * ------------------------------------------------------------------------- */
const rec = {
  engGets: [] as string[],
  lifecycleGets: [] as string[],
  engPosts: [] as string[],
}

async function stubApi(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    if (path === '/api/skills' && method === 'GET') return json({ total: 1, items: [S] })
    const verM = /^\/api\/skills\/([^/]+)\/versions$/.exec(path)
    if (verM && method === 'GET') return json([V])

    const engM = /^\/api\/skills\/([^/]+)\/engineering$/.exec(path)
    if (engM && method === 'GET') {
      rec.engGets.push(path)
      return json(ENG_SKILL)
    }
    const lcM = /^\/api\/skills\/([^/]+)\/lifecycle$/.exec(path)
    if (lcM && method === 'GET') {
      rec.lifecycleGets.push(path)
      return json(LC_SKILL)
    }

    if (path === '/api/skill-candidates' && method === 'GET') return json({ total: 1, items: [C] })
    const cEngM = /^\/api\/skill-candidates\/([^/]+)\/engineering$/.exec(path)
    if (cEngM && method === 'GET') {
      rec.engGets.push(path)
      return json(ENG_CANDIDATE)
    }
    if (cEngM && method === 'POST') {
      rec.engPosts.push(path)
      return json(ENG_CANDIDATE)
    }
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })

    // 外壳噪音。
    if (path === '/api/model-status') return json({ connected: true, model: 'e2e', detail: '' })
    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/runs' && method === 'GET') return json({ total: 0, items: [] })
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    return json({})
  })
}

async function boot(page: Page) {
  rec.engGets = []
  rec.lifecycleGets = []
  rec.engPosts = []
  await page.addInitScript(() => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', 'manager')
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  })
  await stubApi(page)
  await page.goto('/skills')
  await expect(page.getByTestId('skill-workspace')).toBeVisible()
}

async function selectFromGroup(page: Page, group: string) {
  const groupNode = page.locator(`[data-testid="skill-library-group"][data-group="${group}"]`)
  await expect(groupNode).toBeVisible()
  await groupNode.getByTestId('skill-library-item').first().click()
}

/* ------------------------------------------------------------------------- *
 * 用例
 * ------------------------------------------------------------------------- */
test.describe('INC43 S3 —— 技能工程闭环接线', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('技能：中栏真调 engineering + lifecycle，如实渲染六态/评审/用例/评估', async ({ page }) => {
    await boot(page)
    await selectFromGroup(page, 'published')

    // 真发请求（端点可达，消除孤儿能力）。
    await expect.poll(() => rec.engGets).toContain(`/api/skills/${S.id}/engineering`)
    await expect.poll(() => rec.lifecycleGets).toContain(`/api/skills/${S.id}/lifecycle`)

    await expect(page.getByTestId('skill-eng-panel')).toBeVisible()

    // 生命周期：六态徽标 + 合法迁移 + 审批链接。
    const lifecycle = page.getByTestId('skill-eng-lifecycle')
    await expect(lifecycle).toContainText('TESTING')
    await expect(lifecycle).toContainText('可迁移至：REVIEW')
    await expect(lifecycle.getByRole('link', { name: /前往审批/ })).toHaveAttribute(
      'href',
      '/approvals',
    )

    // 评审：severity + findings；must_fix 为空 ⇒「无阻断项」。
    const critique = page.getByTestId('skill-eng-critique')
    await expect(critique).toContainText('severity low')
    await expect(critique).toContainText('缺少边界用例')
    await expect(critique).toContainText('无阻断项')

    // 四类计数（data-category 齐备）+ 逐条 verdict。
    const cats = page.getByTestId('skill-eng-test-cat')
    await expect(cats).toHaveCount(4)
    const values = await cats.evaluateAll((els) =>
      els.map((el) => el.getAttribute('data-category')).sort(),
    )
    expect(values).toEqual(['adversarial', 'boundary', 'normal', 'security'])
    await expect(page.getByTestId('skill-eng-tests')).toContainText('pass')
    await expect(page.getByTestId('skill-eng-tests')).toContainText('fail')

    // 评估：比率按后端原值展示（50.0%），样本 3。
    const evalBlock = page.getByTestId('skill-eng-eval')
    await expect(evalBlock).toContainText('通过率 50.0%')
    await expect(evalBlock).toContainText('样本 3')

    // 既有 P0-12 三计数未受影响（回归对照）。
    await expect(page.getByTestId('skill-card-counts')).toContainText('Tools 1 tools')
  })

  test('候选：无用例 ⇒ 比率一律「—」（非 0%），阻断项逐字，且可触发 POST 闭环', async ({
    page,
  }) => {
    await boot(page)
    await selectFromGroup(page, 'candidate')

    await expect.poll(() => rec.engGets).toContain(`/api/skill-candidates/${C.id}/engineering`)
    await expect(page.getByTestId('skill-eng-panel')).toBeVisible()

    // 阻断项逐字。
    await expect(page.getByTestId('skill-eng-critique')).toContainText('阻断项：补全 goal')
    // 降级原因逐字。
    await expect(page.getByTestId('skill-eng-degraded')).toContainText('沙箱无可用用例')

    // 无用例 ⇒ 四类计数均「—」。
    await expect(page.getByTestId('skill-eng-test-cat').first()).toContainText('—')

    // `sample_size===0` ⇒ 比率一律「—」，**绝不** 0%。
    const evalBlock = page.getByTestId('skill-eng-eval')
    await expect(evalBlock).toContainText('通过率 —')
    await expect(evalBlock).toContainText('已验证通过率 —')
    await expect(evalBlock).not.toContainText('0%')

    // 新按钮触发 `POST /skill-candidates/{id}/engineering`（不含发布）。
    const run = page.getByTestId('skill-eng-run')
    await expect(run).toBeVisible()
    await expect(run).toHaveText('运行工程闭环')
    await run.click()
    await expect.poll(() => rec.engPosts).toContain(`/api/skill-candidates/${C.id}/engineering`)
  })
})
