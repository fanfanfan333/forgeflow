/**
 * inc37_i18n.spec.ts — INC37（全站中文化 / 去代码形态）**运行期直出**回归钉子。
 *
 * 为什么需要它：静态扫描器（`_inc37_i18n_scan.py`）只看 `>文本<` 与展示型字面量，
 * **看不到**「多行 JSX 文案 / `>{expr}` 插值渲染 / 后端枚举直出 / 兜底函数回退原值」。
 * INC37 第 1 轮里 `workflowLabel` 回退原键、`statusBadge` 直出状态、顶栏直出角色枚举
 * 等 4 处漏出，全部是**真实浏览器 DOM** 才逮到的。本钉子把这类「运行期直出」钉死。
 *
 * 口径：桩在 `/api/**` 边界（与既有 spec 同风格），专注「给什么枚举 → 界面出什么中文」。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const BASE = 'http://localhost:4173'
const ISO = '2026-01-01T00:00:00Z'
const RID = 'runscan12345' // 故意不含下划线，避免与「snake_case 检测」互相干扰

const UNKNOWN = {
  role: 'wizard_x',
  workflow: 'mystery_wf',
  status: 'mystery_status',
  outcome: 'mystery_outcome',
  scope: 'phantom_scope',
  actor: 'phantom_actor',
}

/** 后端 `forgeflow/experience/scopes.py` 的全部 actor 取值（D11 防回归用）。 */
const RAW_ACTORS = ['owner', 'system', 'team_member', 'admin', 'everyone']

/** 只在 `/api/**` 边界打桩（形状必须正确，否则视图会踩 undefined 进错误边界）。 */
async function routeApi(
  page: Page,
  unknown: typeof UNKNOWN | null = null,
  actors?: { writable: string[]; readable: string[] },
) {
  await page.route('**/api/**', async (route: Route) => {
    const p = new URL(route.request().url()).pathname
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })
    if (/^\/api\/runs\/[^/]+\/events$/.test(p))
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    if (p === '/api/health') return json({ status: 'ok', database: 'ok', graph: 'ok' })
    if (/^\/api\/agents\/?$/.test(p) || p === '/api/agents/catalog') return json([])
    if (p === '/api/metrics/' || p === '/api/metrics')
      return json({ total_runs: 12, terminal_runs: 10, success_rate: 0.83, avg_latency_ms: 1240, avg_cost_usd: 0.12, total_cost_usd: 1.44, has_data: true, has_cost: true, source: 'hub' })
    if (p === '/api/metrics/evaluation')
      return json({ avg_faithfulness: 8.2, avg_relevance: 8.5, avg_coherence: 8.1, hallucination_rate: 0.012, sample_count: 30 })
    if (p === '/api/metrics/slo') return json({ tiers: [], source: 'hub' })
    if (p === '/api/metrics/runs')
      return json([{ run_id: RID, thread_id: 'thread1', workflow_type: unknown ? unknown.workflow : 'sales_ops', status: unknown ? unknown.status : 'completed', created_at: ISO, completed_at: ISO, total_tokens: 1200, total_cost_usd: 0.5 }])
    if (p === '/api/memory/scopes') {
      const w = actors ? actors.writable : [unknown ? unknown.actor : 'admin']
      const r = actors ? actors.readable : [unknown ? unknown.actor : 'admin']
      return json(['user', 'team', 'episodic', 'semantic', 'org'].map((s) => ({ scope: unknown ? unknown.scope : s, label: '示例层级', description: '示例说明', writable_by: w, readable_by: r, promotable: true })))
    }
    if (p === '/api/memory/list')
      return json({ items: [{ id: 'mem1', scope: unknown ? unknown.scope : 'user', content: '示例记忆内容', namespace: 'ws.default', created_at: ISO }], total: 1 })
    if (p === '/api/experiences')
      return json({ items: [{ id: 'exp1', summary: '示例经验', outcome: unknown ? unknown.outcome : 'success', tags: ['销售'], run_id: RID, created_at: ISO }], total: 1 })
    if (p === '/api/cost/board')
      return json({ has_data: true, currency: 'CNY', tenant_id: 't', total_limit: 100, total_spent: 40, total_pct: 0.4, level: 'ok', budgets: [] })
    if (p === '/api/cost/savings')
      return json({ has_data: true, currency: 'CNY', amount: 20, baseline: 60, actual: 40, multiplier: 1.5, period: '近 30 天' })
    return json({ total: 0, items: [] })
  })
}

/** 注入已登录会话（role 为原始后端枚举串）。 */
async function signIn(page: Page, role: string) {
  await page.addInitScript((r: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', r)
    window.sessionStorage.setItem('forgeflow.user', 'qa')
  }, role)
}

/** 采集可见文本节点（含 ASCII 或中英混排者），去重保序。 */
async function visibleTexts(page: Page): Promise<string[]> {
  const raw: string[] = await page.evaluate(() => {
    const out: string[] = []
    const walk = (n: Node) => {
      const he = n as HTMLElement
      if (he.nodeType === 1) {
        if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(he.tagName)) return
        const chk = (he as unknown as { checkVisibility?: (o?: unknown) => boolean }).checkVisibility
        if (typeof chk === 'function' && !chk.call(he, { checkOpacity: true, checkVisibilityCSS: true })) return
      }
      if (n.nodeType === 3) {
        const t = (n.textContent || '').replace(/\s+/g, ' ').trim()
        if (t && /[A-Za-z\u4e00-\u9fff]/.test(t)) out.push(t)
      }
      n.childNodes.forEach(walk)
    }
    walk(document.body)
    return out
  })
  const seen = new Set<string>()
  return raw.filter((t) => (seen.has(t) ? false : (seen.add(t), true)))
}

// ── 1) 未知枚举 → 全部中文兜底（role / scope / outcome / workflow / status） ──
test('未知枚举一律中文兜底，不得直出原始串', async ({ page }) => {
  await routeApi(page, UNKNOWN)
  await signIn(page, UNKNOWN.role)

  // role → AuthControls 角色标签
  await page.goto('/')
  await expect(page.locator('.auth-role').first()).toHaveText('其他角色')

  // workflow_type + status → 概览近任务表
  await page.goto('/overview')
  const cells = page.locator('table.tbl tbody tr td')
  await expect(cells.filter({ hasText: '其他工作流' })).toHaveCount(1) // D1
  await expect(cells.filter({ hasText: '其他状态' })).toHaveCount(1) // D2

  // scope + outcome → 知识库
  await page.goto('/knowledge')
  const badges = page.locator('.sc-badge.badge, table.tbl tbody td .badge')
  await expect(badges.filter({ hasText: '其他层级' }).first()).toBeVisible() // scopeLabel
  await expect(badges.filter({ hasText: '其他状态' }).first()).toBeVisible() // outcomeLabel

  // actor → 知识库作用域卡「写/读主体」（D11）：未知 actor 走中文兜底「其他」
  await expect(page.locator('.sc-rules').first()).toContainText('其他')

  // 反向：注入的原始串一个都不许出现
  const all = (await visibleTexts(page)).join(' | ')
  for (const k of Object.values(UNKNOWN)) expect(all, `未知串「${k}」不得漏出`).not.toContain(k)
})

// ── 2) 顶栏角色徽标：登录/未登录都不得出现纯 ASCII 角色串（D3） ──
test('顶栏 .org-pill .env 不得直出角色枚举', async ({ page, browser }) => {
  await routeApi(page)
  await signIn(page, 'admin')
  await page.goto('/')
  const env = page.locator('.org-pill .env').first()
  await expect(env).toHaveText('管理员')
  await expect(env, 'env 徽标必须是中文，不能是 admin 这类裸枚举').not.toHaveText(/^[A-Za-z][A-Za-z0-9_]*$/)

  // 未登录：新建独立 context（不注入会话）
  const ctx = await browser.newContext({ baseURL: BASE })
  const p2 = await ctx.newPage()
  await routeApi(p2)
  await p2.goto('/')
  const env2 = p2.locator('.org-pill .env').first()
  await expect(env2).toHaveText('匿名访客')
  await expect(env2).not.toHaveText(/^[A-Za-z][A-Za-z0-9_]*$/)
  await ctx.close()
})

// ── 3) 关键页可见文本：无 snake_case / 无 键=值 / 无 UUID·长十六进制 ──
test('关键页可见文本无代码形态（snake_case / 键=值 / UUID）', async ({ page }) => {
  await routeApi(page)
  await signIn(page, 'admin')

  const SNAKE = /(?<![\w-])[a-z][a-z0-9]*_[a-z0-9_]+(?![\w-])/
  const KV = /\b[a-z_][a-z0-9_.]*\s*=\s*[^\s，,。；;）)]/i
  const UUID = /\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-|\b[0-9a-fA-F]{16,}\b/

  for (const route of ['/tasks', '/', '/overview']) {
    await page.goto(route)
    await expect(page.locator('.org-pill .env').first()).toHaveText('管理员') // D3 全页
    // 健康状态条不得裸出端点路径（应为「服务运行中」）
    await expect(page.locator('.status-bar').first()).not.toContainText('/api/')
    for (const t of await visibleTexts(page)) {
      // 唯一窄白名单：应用自身的运行 ID 展示格式 `wf_xxxx`（不透明标识符，非工程词）。
      const cleaned = t.replace(/\bwf_[a-z0-9]+\b/gi, '')
      expect(cleaned, `[${route}] 可见文本出现 snake_case: «${t}»`).not.toMatch(SNAKE)
      expect(cleaned, `[${route}] 可见文本出现 键=值: «${t}»`).not.toMatch(KV)
      expect(cleaned, `[${route}] 可见文本出现 UUID/长十六进制: «${t}»`).not.toMatch(UUID)
    }
  }
})

// ── 4) D11：知识库「写/读主体」的 actor 枚举必须中文，不得直出 ──
test('知识库作用域的读写主体为中文，不得直出 actor 枚举（D11）', async ({ page }) => {
  // 把后端 `scopes.py` 的全部 actor 取值一次性灌进去，逼出最坏情况。
  await routeApi(page, null, {
    writable: ['owner', 'system'],
    readable: ['team_member', 'admin', 'everyone'],
  })
  await signIn(page, 'admin')
  await page.goto('/knowledge')

  const rule = page.locator('.sc-rules').first()
  await expect(rule).toContainText('本人') // owner
  await expect(rule).toContainText('系统') // system
  await expect(rule).toContainText('团队成员') // team_member
  await expect(rule).toContainText('管理员') // admin
  await expect(rule).toContainText('所有人') // everyone

  // 反向：后端原始 actor 串一个都不许漏到界面上。
  const texts = (await visibleTexts(page)).join(' | ')
  for (const a of RAW_ACTORS) expect(texts, `actor 原始串「${a}」不得漏出`).not.toContain(a)
})
