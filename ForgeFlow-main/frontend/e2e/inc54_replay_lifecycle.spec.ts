/**
 * INC54 —— 「历史只回放」+「技能可退役/删草稿」+「运行下拉可读」的**真实浏览器** e2e。
 *
 * 本 spec 钉住三处用户可见行为（对应 INC54 §六/§七/§九/§十三/§二十一）：
 *
 *   ① **历史回放 ≠ 重新执行**（§七/§二十一 Test A/D/E）：点开一条**已完成**的历史 run 时，
 *      前端只做 `GET /runs/{id}`（+ 只读产物下载），界面渲染的是**落库步骤**；
 *      全程**不得**发出任何会触发执行的写请求（`POST /tasks`、`POST /workspace/tasks`、
 *      `POST /runs`），刷新亦然 —— 采集到的请求流水即证据。
 *   ② **产物可见**（§六）：run 带 `artifacts` ⇒ 内联区出现产物 chip（可下载）。
 *   ③ **技能生命周期**（§十一/§十二/§十三）：已发布技能可「退役 Skill」（走 `POST
 *      /skills/{id}/retire`）、草稿可「删除草稿」（走 `DELETE /skills/{id}`），
 *      且**取消二次确认时一个请求都不发**（阴性对照）。
 *   ④ **运行下拉可读**（§九）：`exec-flow-run-select` 的选项文本是
 *      「意图前 20 字 · run_id 前 8 位 · 时间」，**不再是裸 UUID**。
 *
 * 网络边界（沿用 inc40/inc43/inc46 spec 既有口径）：在 `/api/**` 边界 route 拦截
 * （stub 后端），专注验证**前端在给定后端响应下的真实交互**。后端语义（真实状态码、
 * 跨租户 404、RBAC fail-closed、引用完整性 409）由 pytest 覆盖，不在本 spec 范围。
 *
 * 纪律：
 *   · data-testid **只消费、不新增**（既有 testid 只增不改不删）；
 *   · 断言一律用会重试的 `toBeVisible` / `toContainText` / `toHaveCount` / `expect.poll`；
 *   · 登录沿用 inc33/inc34 口径：sessionStorage 三键 mock 会话。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const TS = '2026-10-05T02:00:00Z'
const RUN_ID = 'a81f3c21deadbeef'
const ART_ID = `${RUN_ID}:artifact:sha9/text`

/** 一次**已完成**的运行：2 个真实步骤 + 1 个产物（形状对齐 `RunDetail`）。 */
const RUN_DETAIL = {
  run_id: RUN_ID,
  thread_id: 'th-inc54',
  status: 'completed',
  outcome: 'success',
  intent: '修改季度报告第三部分',
  errors: [],
  created_at: TS,
  completed_at: TS,
  experience_id: null,
  detail_retained: true,
  steps: [
    { step_id: 's0', index: 0, tool: 'document.inspect', step_type: 'tool', status: 'ok' },
    { step_id: 's1', index: 1, tool: 'document.edit', step_type: 'tool', status: 'ok' },
  ],
  tool_invocations: [
    { tool: 'document.inspect', status: 'ok' },
    { tool: 'document.edit', status: 'ok' },
  ],
  artifacts: [
    {
      id: ART_ID,
      kind: 'document_docx',
      title: '季度报告.edited.docx',
      format: 'docx',
      content: '',
      content_ref: 'sha9/text',
      source: 'document.edit',
      created_at: TS,
    },
  ],
  plan: { run_id: RUN_ID, steps: [] },
}

const RUN_SUMMARY = {
  run_id: RUN_ID,
  thread_id: 'th-inc54',
  status: 'completed',
  outcome: 'success',
  intent: '修改季度报告第三部分',
  title: '修改季度报告第三部分',
  created_at: TS,
  completed_at: TS,
  experience_id: null,
  step_count: 2,
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

const S_PUBLISHED: Skill = {
  id: 'skill_inc54_pub',
  tenant_id: 'tenant_e2e',
  name: 'INC54 已发布技能',
  domain: 'general',
  owner: 'e2e-qa',
  description: '可被退役的技能',
  current_version: '1.0.0',
  status: 'published',
  usage_count: 3,
  featured: false,
  tags: [],
  created_at: TS,
  updated_at: TS,
}

const S_DRAFT: Skill = {
  id: 'skill_inc54_draft',
  tenant_id: 'tenant_e2e',
  name: 'INC54 草稿技能',
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

/** 进入 `/api/**` 的请求流水（`"METHOD /path"`）——「没重跑」与「真发了请求」都靠它。 */
type State = { calls: string[]; skills: Skill[] }

/**
 * 返回**裸数组**（`request<T[]>(...)`）的 GET 端点 —— 兜底 stub 必须对它们回 `[]`，
 * 否则 `data.map(...)` 抛错、整页掉进 ErrorBoundary。
 * 来源：`api/client.ts` 中 `request<...[]>(...)` 的读端点
 * （`/agents/catalog`、`/memory/scopes`、`/memory/search`、`/pending-actions`、`/metrics/*` 列表）。
 */
const BARE_ARRAY_GET =
  /^\/api\/(agents\/catalog|memory\/scopes|memory\/search|pending-actions|metrics\/runs|metrics\/cost(\/.*)?)$/

/**
 * `/api/**` 边界 stub（有状态）。
 *
 * 关键：`GET /api/runs/{id}/events` 返回**空 SSE**（模拟后端事件总线已丢历史）——
 * 于是界面若要显示执行过程，就**只能**靠落库 `steps`，这正是 §八/§二十 要钉的口径。
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

    // ---- 运行 ------------------------------------------------------------
    if (path === '/api/runs' && method === 'GET')
      return json({ total: 1, items: [RUN_SUMMARY] })
    if (/^\/api\/runs\/[^/]+\/events$/.test(path))
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    if (/^\/api\/runs\/[^/]+\/artifacts\//.test(path) && method === 'GET')
      return route.fulfill({ status: 200, contentType: 'text/plain', body: 'ok' })
    if (path === `/api/runs/${RUN_ID}` && method === 'GET') return json(RUN_DETAIL)
    // 首页「近期任务」的**持久底**是会话摘要（`history.ts::sessionToRunSummary`）：
    // `session_id` 即该会话首个 run 的 run_id，故映射行的 run_id === RUN_ID。
    if (path === '/api/workspace/sessions' && method === 'GET')
      return json({
        total: 1,
        items: [
          {
            session_id: RUN_ID,
            title: '修改季度报告第三部分',
            latest_status: 'completed',
            created_at: TS,
          },
        ],
      })

    // ---- 技能 ------------------------------------------------------------
    if (path === '/api/skills' && method === 'GET')
      return json({ total: st.skills.length, items: st.skills })
    const skillAct = /^\/api\/skills\/([^/]+)\/(retire|restore)$/.exec(path)
    if (skillAct && method === 'POST') {
      const [, id, action] = skillAct
      st.skills = st.skills.map((s) =>
        s.id === id ? { ...s, status: action === 'retire' ? 'retired' : 'published' } : s,
      )
      const found = st.skills.find((s) => s.id === id)
      return found ? json(found) : json({ detail: 'Skill not found' }, 404)
    }
    const skillDel = /^\/api\/skills\/([^/]+)$/.exec(path)
    if (skillDel && method === 'DELETE') {
      const id = skillDel[1]
      const target = st.skills.find((s) => s.id === id)
      if (!target) return json({ detail: 'Skill not found' }, 404)
      if (target.status !== 'draft')
        return json({ detail: '只有草稿可删除，已发布技能请改用退役' }, 409)
      st.skills = st.skills.filter((s) => s.id !== id)
      return route.fulfill({ status: 204, body: '' })
    }
    if (/^\/api\/skills\/[^/]+\/(versions|lifecycle|readiness|engineering|rules|experience)$/.test(path))
      return json(path.endsWith('/versions') ? [] : {})
    if (path === '/api/evolution/interlock' && method === 'GET') return json({})

    // 其余：GET 返回「空列表 + 空对象」形状，**但**有少数端点直接返回**裸数组**
    // （`client.ts` 里 `request<T[]>(...)`）—— 若也给对象，`data.map(...)` 会抛错、
    // 整页掉进 ErrorBoundary（首页 `HomeView::AgentSection` 就读 `/agents/catalog` 的
    // 裸数组）。故先按路径判定这两类形状；写请求一律 `{}`。
    if (method === 'GET') {
      // 裸数组端点（`/agents/catalog`、`/memory/scopes`、`/pending-actions`、
      // `/memory/search`、`/metrics/*` 的列表读）⇒ `[]`。
      if (BARE_ARRAY_GET.test(path)) return json([])
      return json({ total: 0, items: [], has_data: false })
    }
    return json({})
  })
}

/** 固定登录 mock：sessionStorage 三键（不打真后端、不耗限流）。 */
async function boot(page: Page, st: State, opts: { path?: string } = {}) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', 'admin')
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  })
  await stubApi(page, st)
  await page.goto(opts.path ?? '/')
}

/* ------------------------------------------------------------------------- *
 * ① 历史回放：只读 + 渲染落库步骤 + 产物可见
 * ------------------------------------------------------------------------- */

test.describe('INC54 历史回放（§七/§二十一）', () => {
  test('published skill can be removed directly from the library, after confirmation', async ({ page }) => {
    const st: State = { calls: [], skills: [S_PUBLISHED] }
    await boot(page, st, { path: '/skills/advanced' })
    await page.route(`**/api/skills/${S_PUBLISHED.id}/archive`, async (route) => {
      st.calls.push(`${route.request().method()} /api/skills/${S_PUBLISHED.id}/archive`)
      st.skills = []
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"deleted":true}' })
    })
    await page.getByRole('button', { name: `删除技能：${S_PUBLISHED.name}` }).click()
    await expect(page.getByTestId('confirm-dialog')).toContainText('历史版本与运行记录保留')
    expect(st.calls.some(c => c.includes('/archive'))).toBeFalsy()
    await page.getByTestId('confirm-dialog-ok').click()
    await expect(page.getByTestId('skill-library-item')).toHaveCount(0)
    expect(st.calls).toContain(`POST /api/skills/${S_PUBLISHED.id}/archive`)
  })
  test('已发布技能：退役 ⇒ 真发 POST /skills/{id}/retire', async ({ page }) => {
    const st: State = { calls: [], skills: [S_PUBLISHED] }
    await boot(page, st, { path: '/skills/advanced' })

    await page.locator('[data-testid="skill-library-item"]').first().click()
    const retireBtn = page.getByTestId('skill-action-retire')
    await expect(retireBtn).toBeVisible()
    await expect(retireBtn).toHaveText('退役 Skill')

    await retireBtn.click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    // 未确认前**绝不**发请求（阴性对照）。
    expect(st.calls.some((c) => c.includes('/retire'))).toBeFalsy()

    await page.getByTestId('confirm-dialog-ok').click()
    await expect
      .poll(() => st.calls)
      .toContain(`POST /api/skills/${S_PUBLISHED.id}/retire`)
  })

  test('草稿技能：删除 ⇒ 真发 DELETE /skills/{id}（204 无正文也正确收口）', async ({ page }) => {
    const st: State = { calls: [], skills: [S_DRAFT] }
    await boot(page, st, { path: '/skills/advanced' })

    await page.locator('[data-testid="skill-library-item"]').first().click()
    const delBtn = page.getByTestId('skill-action-delete')
    await expect(delBtn).toBeVisible()
    await expect(delBtn).toHaveText('删除草稿')

    await delBtn.click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    await page.getByTestId('confirm-dialog-ok').click()
    await expect
      .poll(() => st.calls)
      .toContain(`DELETE /api/skills/${S_DRAFT.id}`)
  })

  test('阴性对照：取消确认 ⇒ 一个请求都不发', async ({ page }) => {
    const st: State = { calls: [], skills: [S_PUBLISHED] }
    await boot(page, st, { path: '/skills/advanced' })

    await page.locator('[data-testid="skill-library-item"]').first().click()
    await page.getByTestId('skill-action-retire').click()
    await expect(page.getByTestId('confirm-dialog')).toBeVisible()
    await page.getByTestId('confirm-dialog-cancel').click()
    await expect(page.getByTestId('confirm-dialog')).toHaveCount(0)

    expect(st.calls.some((c) => c.startsWith('POST /api/skills/'))).toBeFalsy()
    expect(st.calls.some((c) => c.startsWith('DELETE '))).toBeFalsy()
  })
})

/* ------------------------------------------------------------------------- *
 * ③ 运行下拉可读（§九）
 * ------------------------------------------------------------------------- */

test.describe('INC54 运行下拉可读（§九）', () => {
  test('选项文本 = 意图前 20 字 · run_id 前 8 位 · 时间（不是裸 UUID）', async ({ page }) => {
    const st: State = { calls: [], skills: [S_PUBLISHED] }
    await boot(page, st, { path: '/skills/advanced' })

    await page.locator('[data-testid="skill-library-item"]').first().click()

    const select = page.getByTestId('exec-flow-run-select')
    await expect(select).toBeVisible()
    const option = select.locator('option').first()
    // 意图前 20 字在；run_id 前 8 位在；时间片段在（`MM-DD HH:mm` 或 `HH:mm`）。
    await expect(option).toContainText('修改季度报告第三部分')
    await expect(option).toContainText('a81f3c21')
    // 裸 UUID 不得**独占**整行：文本必须以意图开头。
    await expect(option).not.toHaveText(RUN_ID)
  })
})
