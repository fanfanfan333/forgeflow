/**
 * INC40 —— 「删除 / 归档」前端可达性 **真实浏览器** e2e（`frontend/e2e`）。
 *
 * 背景（本仓教训）：工程师自述「已接线」不等于「行为被真实浏览器钉住」。INC40 的三处
 * 改动（rat-ified 问题）分别是：
 *
 *   1. **资源删除**（`ResourcePicker` → `DELETE /resources/{id}`）：确认框 → 真发请求
 *      → 卡片从列表消失（前端可达，且是唯一入口）。
 *   2. **预算写面**（`CostView::BudgetForm` → `POST /cost/budgets` upsert；
 *      `BudgetItem` → `DELETE /cost/budgets/{scope}`）：manager/admin 经 `write:metrics`
 *      可达的建删闭环。
 *   3. **记忆归档**（`KnowledgeView::MemoryRow` → `POST /memory/{id}/archive`）：
 *      平台**从不物理删除**记忆，UI 措辞一律「归档」而非「删除」（pgvector 的
 *      `DELETE /memory/{id}` 仍不可达 —— INC41 候选）。
 *
 * 本 spec 为以上三项**新增**回归钉子（只增不改不删既有 testid；新增断言一律用会重试的
 * `toHaveText` / `toBeVisible` / `expect.poll`）。
 *
 * 网络边界（分层诚实，沿用 inc26/inc33/inc36 spec 口径）：在 `/api/**` 边界做 route 拦截
 * （stub 后端），专注验证**前端在给定后端响应下的真实交互** —— 即「点击 → 确认 → 真发
 * 请求 → 界面如实更新」这一条前端可达链。后端语义（200/404/403 的真实状态码、跨租户
 * 404、RBAC fail-closed、预算自然键 upsert、`DELETE 0` 不当成功）由
 * `tests/unit/test_inc40_cost_budget_api.py`、`tests/unit/test_inc40_delete_rbac.py`、
 * `tests/unit/test_inc40_memory_archive.py` 与
 * `tests/integration/test_inc40_resource_delete.py` 用真实 HTTP / DB 覆盖。
 *
 * 角色：mock 会话（sessionStorage `forgeflow.jwt` / `forgeflow.role`），不打真后端、
 * 不消耗登录限流。资源删除按钮与预算面板均要求 `manager` 及以上（与后端
 * `("DELETE","/resources") ⇒ write:skills`、`("POST","/cost") ⇒ write:metrics` 同档），
 * 故统一以 `admin` 身份（`roleGate.ts::ROLE_RANK` 中 admin=3 ≥ manager=2）驱动；
 * `/cost` 路由的守卫亦为 `admin`。
 *
 * 引文纪律：一律 `文件名::符号名`（见 ResourcePicker.tsx::ResourcePicker）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 最小夹具（与后端响应形状同形；数字全部来自 stub，绝不臆造）
 * ------------------------------------------------------------------------- */

type BudgetSeed = {
  scope: 'tenant' | 'team' | 'task'
  scope_id: string | null
  limit: number | null
}

type ResourceSeed = {
  id: string
  kind: string
  name: string
  status: string
  summary?: Record<string, unknown>
}

type MemorySeed = {
  id: string
  tenant_id: string | null
  scope: string
  content: string
  team_id: string | null
  namespace: string
  metadata: Record<string, unknown>
  created_at: string
}

const RES_A: ResourceSeed = { id: 'res_a01', kind: 'file', name: 'leads.csv', status: 'parsed' }
const RES_B: ResourceSeed = {
  id: 'res_b02',
  kind: 'database',
  name: 'public.orders',
  status: 'registered',
}

const MEM_A: MemorySeed = {
  id: 'mem_a01',
  tenant_id: 'tenant_e2e',
  scope: 'semantic',
  content: 'INC40 记忆归档钉子：本条仅用于验证归档链路。',
  team_id: null,
  namespace: 'workspace/tenant_e2e',
  metadata: {},
  created_at: new Date().toISOString(),
}

const LIMITS = {
  max_bytes: 1048576,
  supported_extensions: ['.csv', '.tsv', '.txt', '.md', '.json'],
  note: '上限与类型白名单来自后端单一事实源，前端不得写死',
}

/**
 * 每个测试的可变服务端状态。stub 直接读写它，从而让「前端在一次交互后重新拉取」
 * 得到的响应反映上一步的真实效果（例如删除后列表真的少一项）。
 */
type State = {
  resources: ResourceSeed[]
  budgets: BudgetSeed[]
  memories: MemorySeed[]
  /** 进入 `/api/**` 的请求流水（`"METHOD /path"`，用于「确认真发了请求」与「取消不发请求」）。 */
  calls: string[]
}

function freshState(opts: Partial<State> = {}): State {
  return {
    resources: opts.resources ?? [],
    budgets: opts.budgets ?? [],
    memories: opts.memories ?? [],
    calls: [],
  }
}

/** 由当前预算状态合成 `GET /cost/board` 的响应（`CostBoard`，与前端类型逐字段对齐）。 */
function boardPayload(st: State) {
  const limit = st.budgets.reduce((s, b) => s + (b.limit ?? 0), 0)
  return {
    has_data: st.budgets.length > 0,
    currency: 'CNY',
    tenant_id: 'tenant_e2e',
    total_limit: st.budgets.length > 0 ? limit : null,
    total_spent: 0,
    total_pct: null,
    level: 'ok',
    budgets: st.budgets.map((b) => ({
      scope: b.scope,
      scope_id: b.scope_id,
      limit: b.limit,
      spent: 0,
      pct: null,
      level: 'ok',
    })),
  }
}

/** 一条预算的 `POST /cost/budgets` 响应（`BudgetRow`，与前端类型逐字段对齐）。 */
function budgetRowFrom(body: {
  scope: 'tenant' | 'team' | 'task'
  scope_id?: string | null
  limit_amount: number
}) {
  return {
    id: `bud_${body.scope}_${body.scope_id ?? 'all'}`,
    scope: body.scope,
    scope_id: body.scope_id ?? null,
    limit_amount: body.limit_amount,
    warn_ratio: 0.8,
    currency: 'CNY',
    on_exceed: [],
    created_at: new Date().toISOString(),
  }
}

/**
 * `/api/**` 边界 stub（有状态）：把「删除 / 归档」真实作用到 `st` 上，让前端的
 * 失效重取（TanStack Query invalidate）看到更新后的数据。
 */
async function stubApi(page: Page, st: State) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    st.calls.push(`${method} ${path}`)

    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // ---- 资源 -------------------------------------------------------------
    if (path === '/api/resources/limits' && method === 'GET') return json(LIMITS)
    if (path === '/api/resources' && method === 'GET')
      return json({ total: st.resources.length, items: st.resources })
    const resPreview = /^\/api\/resources\/[^/]+\/preview$/.exec(path)
    if (resPreview && method === 'GET')
      return json({
        id: 'res_a01',
        kind: 'file',
        available: true,
        format: 'table',
        columns: ['a'],
        rows: [['1']],
        content: '',
        truncated: false,
        note: '',
      })
    const resDel = /^\/api\/resources\/([^/]+)$/.exec(path)
    if (resDel && method === 'DELETE') {
      const id = resDel[1]
      const before = st.resources.length
      st.resources = st.resources.filter((r) => r.id !== id)
      if (st.resources.length === before) return json({ detail: 'Resource not found' }, 404)
      return json({ deleted: true, resource_id: id })
    }

    // ---- 成本看板 / 节省 ---------------------------------------------------
    if (path === '/api/cost/board' && method === 'GET') return json(boardPayload(st))
    if (path === '/api/cost/savings' && method === 'GET')
      return json({
        has_data: false,
        currency: 'CNY',
        amount: null,
        baseline: null,
        actual: null,
        multiplier: 1,
        period: '',
      })

    // ---- 预算写面（upsert + delete）---------------------------------------
    if (path === '/api/cost/budgets' && method === 'POST') {
      const body = JSON.parse(req.postData() ?? '{}') as {
        scope: 'tenant' | 'team' | 'task'
        scope_id?: string | null
        limit_amount: number
      }
      const scopeId = body.scope_id ?? null
      st.budgets = st.budgets.filter(
        (b) => !(b.scope === body.scope && (b.scope_id ?? null) === scopeId),
      )
      st.budgets.push({ scope: body.scope, scope_id: scopeId, limit: body.limit_amount })
      return json(budgetRowFrom(body))
    }
    const budDel = /^\/api\/cost\/budgets\/([^/]+)$/.exec(path)
    if (budDel && method === 'DELETE') {
      const scope = budDel[1]
      const scopeId = url.searchParams.get('scope_id')
      const before = st.budgets.length
      st.budgets = st.budgets.filter(
        (b) => !(b.scope === scope && (b.scope_id ?? null) === (scopeId ?? null)),
      )
      if (st.budgets.length === before) return json({ detail: 'Budget not found' }, 404)
      return json({ deleted: true, scope, scope_id: scopeId })
    }

    // ---- 记忆（只归档，绝不删除）-----------------------------------------
    if (path === '/api/memory/scopes' && method === 'GET')
      return json([
        { scope: 'user', label: '用户', description: '', writable_by: [], readable_by: [], promotable: false },
        { scope: 'team', label: '团队', description: '', writable_by: [], readable_by: [], promotable: true },
        { scope: 'episodic', label: '情景', description: '', writable_by: [], readable_by: [], promotable: false },
        { scope: 'semantic', label: '语义', description: '', writable_by: [], readable_by: [], promotable: false },
        { scope: 'org', label: '组织', description: '', writable_by: [], readable_by: [], promotable: true },
      ])
    if (path === '/api/memory' && method === 'GET')
      return json({ total: st.memories.length, items: st.memories })
    const memArch = /^\/api\/memory\/([^/]+)\/archive$/.exec(path)
    if (memArch && method === 'POST') {
      const id = memArch[1]
      const before = st.memories.length
      st.memories = st.memories.filter((m) => m.id !== id)
      if (st.memories.length === before) return json({ detail: 'Memory not found' }, 404)
      return json({ archived: true, memory_id: id })
    }
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })

    // ---- 运行列表 / 会话 / 事件流（本 spec 不验证实时流）------------------
    if (path === '/api/runs' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/workspace/sessions' && method === 'GET')
      return json({ total: 0, items: [] })
    if (/^\/api\/runs\/[^/]+\/events$/.test(path))
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })

    // 其余（其它面板 / 会话杂项）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

/** 固定登录 mock：sessionStorage 三键（不打真后端、不耗限流）。 */
async function boot(
  page: Page,
  st: State,
  opts: { role?: string; path?: string } = {},
) {
  await page.addInitScript((role: string) => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', role)
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  }, opts.role ?? 'admin')
  await stubApi(page, st)
  await page.goto(opts.path ?? '/runs')
}

/* ------------------------------------------------------------------------- *
 * ① 资源删除：确认框 → 真发 DELETE → 卡片从列表消失
 * ------------------------------------------------------------------------- */

test.describe('INC40 资源删除（前端可达）', () => {
  test('点删除 → 确认框 → 真发 DELETE /resources/{id} → 卡片移除', async ({ page }) => {
    const st = freshState({ resources: [RES_A, RES_B] })
    await boot(page, st, { path: '/runs' })

    // 两张资源卡就位，每张一个删除入口（manager+ 才渲染）。
    await expect(page.getByTestId('resource-card')).toHaveCount(2)
    const delButtons = page.getByTestId('resource-delete')
    await expect(delButtons).toHaveCount(2)

    // 点第一张卡的删除 → 二次确认弹层出现（未确认前绝不发请求）。
    await page.getByTestId('resource-card').first().getByRole('button', { name: '删除' }).click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    expect(st.calls.some((c) => c.startsWith('DELETE /api/resources'))).toBeFalsy()

    // 确认 → 真发 DELETE，且卡片真的少一张。
    await page.getByTestId('confirm-dialog-ok').click()
    await expect.poll(() => st.calls).toContain('DELETE /api/resources/res_a01')
    await expect(page.getByTestId('resource-card')).toHaveCount(1)
    await expect(page.getByTestId('confirm-dialog')).toHaveCount(0)
  })

  // 角色可见性（§7 钉子）：viewer 看不到删除入口（改由后端 RBAC fail-closed 兜底）。
  test('viewer 身份：资源卡不渲染删除按钮（无 403 误触）', async ({ page }) => {
    const st = freshState({ resources: [RES_A, RES_B] })
    await boot(page, st, { role: 'viewer', path: '/runs' })

    // 资源列表仍可见（读者可见），但破坏性入口对无权角色隐藏。
    await expect(page.getByTestId('resource-card')).toHaveCount(2)
    await expect(page.getByTestId('resource-delete')).toHaveCount(0)
  })

  // 负向对照：取消（含 Escape）绝不发请求、卡片不变。
  test('取消确认框：不发任何请求、卡片数量不变', async ({ page }) => {
    const st = freshState({ resources: [RES_A, RES_B] })
    await boot(page, st, { path: '/runs' })

    await page.getByTestId('resource-card').first().getByRole('button', { name: '删除' }).click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    await page.getByTestId('confirm-dialog-cancel').click()
    await expect(page.getByTestId('confirm-dialog')).toHaveCount(0)
    await expect(page.getByTestId('resource-card')).toHaveCount(2)
    expect(st.calls.some((c) => c.startsWith('DELETE '))).toBeFalsy()
  })
})

/* ------------------------------------------------------------------------- *
 * ② 预算建删闭环（POST upsert + DELETE）
 * ------------------------------------------------------------------------- */

test.describe('INC40 预算建删闭环（manager 可达）', () => {
  test('空看板 → 表单建租户预算（POST）→ 行内删除（DELETE）', async ({ page }) => {
    const st = freshState({ budgets: [] })
    await boot(page, st, { role: 'admin', path: '/cost' })

    // 表单就位；初始无预算 ⇒ 无删除按钮。
    const form = page.getByTestId('budget-form')
    await expect(form).toBeVisible()
    await expect(page.getByTestId('budget-delete')).toHaveCount(0)

    // 建一条租户预算（默认层级=租户，仅需金额）。
    await page.getByTestId('budget-limit').fill('1000')
    await page.getByTestId('budget-submit').click()
    await expect.poll(() => st.calls).toContain('POST /api/cost/budgets')

    // 失效重取后该行出现 ⇒ 删除入口可达。
    await expect(page.getByTestId('budget-delete')).toHaveCount(1)
    await page.getByTestId('budget-delete').click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    await page.getByTestId('confirm-dialog-ok').click()
    await expect.poll(() => st.calls).toContain('DELETE /api/cost/budgets/tenant')

    // 删除后该行消失（看板重取为空 ⇒ 回到诚实空态）。
    await expect(page.getByTestId('budget-delete')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ③ 记忆归档：措辞「归档」、发 POST /archive、绝不发 DELETE
 * ------------------------------------------------------------------------- */

test.describe('INC40 记忆归档（措辞诚实，绝不物理删除）', () => {
  test('点归档 → 确认按钮逐字「归档」→ 真发 POST /memory/{id}/archive → 移出默认列表', async ({
    page,
  }) => {
    const st = freshState({ memories: [MEM_A] })
    await boot(page, st, { role: 'admin', path: '/knowledge' })

    // 条目在列；操作列按钮逐字「归档」（不是「删除」）。
    const archiveBtn = page.getByTestId('memory-archive')
    await expect(archiveBtn).toHaveCount(1)
    await expect(archiveBtn).toHaveText('归档')

    await archiveBtn.click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    // 确认按钮逐字「归档」——平台从不物理删除，措辞绝不写成「删除」。
    await expect(page.getByTestId('confirm-dialog-ok')).toHaveText('归档')
    await expect(page.getByTestId('confirm-dialog')).toContainText('归档该条记忆')

    await page.getByTestId('confirm-dialog-ok').click()
    await expect.poll(() => st.calls).toContain('POST /api/memory/mem_a01/archive')

    // 归档后移出默认列表（诚实空态），且**从未**发出任何 DELETE。
    await expect(page.getByText('该层级暂无记忆')).toBeVisible()
    expect(st.calls.some((c) => c.startsWith('DELETE /api/memory'))).toBeFalsy()
  })
})

/* ------------------------------------------------------------------------- *
 * ④ 营销文案已纠正（T04）：不再宣称「级联删除 / 被遗忘权」
 * ------------------------------------------------------------------------- */

test.describe('INC40 文案纠正：能力如实（无「级联删除 / 被遗忘权」）', () => {
  test('落地页：审计与留存如实描述为「按租户隔离删除」', async ({ page }) => {
    const st = freshState()
    await stubApi(page, st)
    await page.goto('/welcome')
    await expect(page.getByText('资源与预算的删除按租户隔离')).toBeVisible()
    // 旧文案（营销 ≠ 机制）必须消失。
    await expect(page.getByText('级联删除')).toHaveCount(0)
    await expect(page.getByText('被遗忘权')).toHaveCount(0)
  })

  test('架构页：删除与留存如实描述，旧「被遗忘权」条目消失', async ({ page }) => {
    const st = freshState()
    await stubApi(page, st)
    await page.goto('/architecture')
    await expect(page.getByText('按租户删除资源/预算 · 越权拒绝')).toBeVisible()
    await expect(page.getByText('被遗忘权')).toHaveCount(0)
  })
})

/* ------------------------------------------------------------------------- *
 * ⑤ 既有 testid 回归（只增未改：ResourcePicker 既有入口仍可寻址）
 * ------------------------------------------------------------------------- */

test.describe('INC40 回归：既有资源 testid 仍可寻址', () => {
  test('ResourcePicker 既有入口未因本次改动失效', async ({ page }) => {
    const st = freshState({ resources: [RES_A] })
    await boot(page, st, { path: '/runs' })
    await expect(page.getByTestId('resource-add')).toHaveCount(1)
    await expect(page.getByTestId('resource-kind-filter')).toHaveCount(1)
    await expect(page.getByTestId('resource-list')).toHaveCount(1)
    await expect(page.getByTestId('resource-card')).toHaveCount(1)
  })
})
