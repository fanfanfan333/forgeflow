/**
 * INC43 / T05 —— 诚实性 + testid 零回归的**真实浏览器**交叉确认 spec。
 *
 * 目标（PRD §4.5 附录 A「只增不改不删」+ 诚实纪律 P0-2 / P0-4）：
 *   ① 首页首屏无「任务」概念（`getByText('新任务')` 可见数 0）——对既有
 *      `inc43_chat_workspace.spec.ts` 的独立交叉确认；
 *   ② 诚实空态：无数据 / 无产物时相关区域渲染「—」，且**不出现** `0%` / `0 次`
 *      之类的假计数（在**页面可见文本**上用正则断言，不只测选择器存在）；
 *   ③ 零回归可见性：`chat-workspace` / `chat-composer` / `skill-workspace` /
 *      `skill-library` / `skill-inspector` 逐字存在且可见；
 *   ④ 产物四件套不漂移：出现产物区时 `artifact-card` / `artifact-preview` /
 *      `artifact-download` 各恒为 1，「无产物」时 `artifact-empty` 恒为 1
 *      （照 `inc32_workspace.spec.ts` 的断言口径）。
 *
 * ── 判定纪律（本 spec 严格遵守）────────────────────────────────────────────
 *   · **绝不**用 `getByTestId('X').toHaveCount(0)` 去断言「不存在」——若 X 实为
 *     className 会**永真不红**。断言「不存在」时改为：断言**真实替代事实** + 阳性对照。
 *   · 本 spec 消费到的 `artifact-*` / `chat-doc-diff-*` 均为**真实** `data-testid`
 *     属性，故其 `toHaveCount` 是有效判定。
 *   · 断言一律用会重试的 `toBeVisible` / `toHaveText` / `toContainText` /
 *     `toHaveCount` / `expect.poll`，**禁用 `allTextContents`**。
 *
 * 网络边界（沿用 inc32/inc41/inc43 spec 既有口径）：在 `/api/**` 边界 route 打桩，
 * 专注验证**前端在给定后端响应下的真实交互**；后端语义不在本 spec 范围。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

/* ------------------------------------------------------------------------- *
 * 夹具
 * ------------------------------------------------------------------------- */

const RUN_ID = 'run_inc43_honesty_1'
const DOCX_ID = 'art_inc43_diff_1'
const INTENT = '把方案改成正式版'
const REPLY = '已完成修改。'

type Artifact = {
  id: string
  kind: string
  title: string
  format: string
  content: string
  source: string
  result_ref: string
  created_at: string
  content_ref?: string
  /** `docx` 产物的修改摘要；`removed` / `numeric_changes` 可**缺失**（⇒ 界面「—」）。 */
  diff?: Record<string, unknown>
}

type StepLike = { index: number; tool?: string; note?: string; status?: string }

/** `/tasks` 左列可被选中的 run（含详情；`summaryOf` 派生列表项）。 */
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
  steps: StepLike[]
  errors: string[]
  tool_invocations: Record<string, unknown>[]
  artifacts: Artifact[]
  session_id: string
  parent_run_id: string | null
}

type Cfg = {
  /** 首页发送任务后返回并订阅的 run 的 `GET /runs/{id}` status。 */
  status: string
  /** `/events` 的 SSE 原始帧；`null` ⇒ 500（走轮询兜底 ⇒ 恒「运行中」）。 */
  sseBody: string | null
  steps: StepLike[]
  tools: Record<string, unknown>[]
  skills: { id: string; version: string; name: string }[]
  artifacts: Artifact[]
  /** `/tasks` 左列 seed（含详情）；默认空 ⇒ 「近期任务」诚实空态。 */
  tasksRuns: RunLike[]
  /** `GET /metrics/` 的 `has_data`（决定 KPI 是否渲染真实数字）。 */
  hasData: boolean
  /** `GET /skills` 列表项。 */
  skillItems: unknown[]
}

function defaults(): Cfg {
  return {
    status: 'running',
    sseBody: null,
    steps: [],
    tools: [],
    skills: [],
    artifacts: [],
    tasksRuns: [],
    hasData: false,
    skillItems: [],
  }
}

const cfg: Cfg = defaults()

function frame(type: string, data: Record<string, unknown>, seq: number) {
  return { run_id: RUN_ID, type, data, seq, ts: '2026-01-01T00:00:00Z' }
}

/** 把一组帧包成 SSE 文本（末帧 `[DONE]`，与后端收尾一致）。 */
function sse(frames: unknown[]): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'
}

function makeRun(over: Partial<RunLike> & { run_id: string }): RunLike {
  return {
    run_id: over.run_id,
    thread_id: over.thread_id ?? `thread_${over.run_id}`,
    status: over.status ?? 'completed',
    outcome: over.outcome ?? 'success',
    intent: over.intent ?? '任务',
    title: over.title ?? over.intent ?? '任务',
    created_at: over.created_at ?? '2026-01-01T00:00:00Z',
    completed_at: over.completed_at ?? null,
    experience_id: over.experience_id ?? null,
    steps: over.steps ?? [],
    errors: over.errors ?? [],
    tool_invocations: over.tool_invocations ?? [],
    artifacts: over.artifacts ?? [],
    session_id: over.session_id ?? '',
    parent_run_id: over.parent_run_id ?? null,
  }
}

/** `RunLike` → `RunSummary`（`GET /runs` 列表项形状）。 */
function summaryOf(d: RunLike) {
  return {
    run_id: d.run_id,
    thread_id: d.thread_id,
    status: d.status,
    outcome: d.outcome,
    intent: d.intent,
    title: d.title,
    created_at: d.created_at,
    completed_at: d.completed_at,
    experience_id: d.experience_id,
    step_count: d.steps.length,
    session_id: d.session_id,
    parent_run_id: d.parent_run_id,
  }
}

/* ------------------------------------------------------------------------- *
 * API stub（`/api/**` 边界拦截；一个处理器覆盖 首页 / `/tasks` / `/skills`）
 * ------------------------------------------------------------------------- */

async function stub(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 首页：异步派发句柄（提交真发的就是它）。
    if (path === '/api/workspace/tasks' && method === 'POST') {
      return json({
        run_id: RUN_ID,
        thread_id: 'thread-1',
        status: cfg.status,
        detail: {},
        session_id: 'sess-1',
        parent_run_id: null,
      })
    }

    // 运行事件流：给定帧 / 500（触发 `useRunEvents` 轮询兜底 ⇒ 保持「运行中」）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      if (cfg.sseBody == null) {
        return route.fulfill({ status: 500, contentType: 'text/plain', body: 'sse down' })
      }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: cfg.sseBody })
    }

    // 产物原文（认证 fetch 的目标）。
    const artM = /^\/api\/runs\/([^/]+)\/artifacts\/([^/]+)$/.exec(path)
    if (artM && method === 'GET') {
      const d =
        cfg.tasksRuns.find((r) => r.run_id === artM[1]) ??
        ({
          artifacts: cfg.artifacts,
        } as RunLike)
      const a = d.artifacts.find((x) => x.id === artM[2])
      return route.fulfill({
        status: 200,
        contentType: 'text/markdown',
        body: a?.content ?? '',
      })
    }

    // 运行详情：首页 run（RUN_ID）由 cfg 现场装配；`/tasks` 的 run 直接取 seed。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      const id = detailM[1]
      if (id === RUN_ID) {
        return json({
          run_id: RUN_ID,
          thread_id: 'thread-1',
          status: cfg.status,
          outcome: cfg.status === 'completed' ? 'success' : '',
          intent: INTENT,
          steps: cfg.steps,
          errors: [],
          created_at: '2026-01-01T00:00:00Z',
          completed_at: cfg.status === 'completed' ? '2026-01-01T00:01:00Z' : null,
          experience_id: null,
          runtime_mode: 'llm',
          tool_invocations: cfg.tools,
          codeplane: { injected: { skills: cfg.skills, memory: [] } },
          artifacts: cfg.artifacts,
          session_id: 'sess-1',
          parent_run_id: null,
        })
      }
      const seeded = cfg.tasksRuns.find((r) => r.run_id === id)
      if (seeded) return json({ ...seeded, codeplane: {} })
      return json({ detail: 'not found' }, 404)
    }

    // 运行列表（首页 KPI / 近期任务 + `/tasks` 左列**同源**）。
    if (path === '/api/runs' && method === 'GET') {
      return json({ total: cfg.tasksRuns.length, items: cfg.tasksRuns.map(summaryOf) })
    }

    // 会话列表。
    if (path === '/api/workspace/sessions' && method === 'GET') {
      return json({ total: 0, items: [] })
    }

    // 指标 / 成本（KPI 诚实空态的数据源）。
    if (path === '/api/metrics/' || path === '/api/metrics') {
      return json({ has_data: cfg.hasData })
    }
    if (path === '/api/cost/savings') return json({ has_data: false })

    // 智能体 / 技能 / 候选 / 经验（技能资产中心数据源）。
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/skills' && method === 'GET') {
      return json({ total: cfg.skillItems.length, items: cfg.skillItems })
    }
    if (/^\/api\/skills\/[^/]+\/versions$/.test(path)) return json([])
    if (path === '/api/skill-candidates' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/experiences' && method === 'GET') return json({ total: 0, items: [] })

    // 外壳噪音（避免无关 4xx）。
    if (path === '/api/model-status') return json({ connected: true, model: 'e2e', detail: '' })
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    // 其余一律诚实空。
    return json({ total: 0, items: [] })
  })
}

async function signIn(page: Page) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    window.sessionStorage.setItem('forgeflow.role', 'admin')
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  })
}

function reset(over: Partial<Cfg>) {
  Object.assign(cfg, defaults(), over)
}

/** 打开首页（默认无 run：仅欢迎态 / 诚实空态）。 */
async function bootHome(page: Page, over: Partial<Cfg> = {}) {
  reset(over)
  await signIn(page)
  await stub(page)
  await page.goto('/')
  await expect(page.getByTestId('chat-workspace')).toBeVisible()
}

/** 打开任意 SPA 路由（同一进程内 stub 持续生效，可多路由串联）。 */
async function bootRoute(page: Page, path: string, over: Partial<Cfg> = {}) {
  reset(over)
  await signIn(page)
  await stub(page)
  await page.goto(path)
}

/** 在富输入区提交一条消息。 */
async function send(page: Page, text: string) {
  await page.getByTestId('chat-composer-input').fill(text)
  await page.getByTestId('chat-composer-send').click()
}

/* ------------------------------------------------------------------------- *
 * ① 首页首屏无「任务」概念
 * ------------------------------------------------------------------------- */
test.describe('INC43 T05 —— 诚实性 + testid 零回归', () => {
  test('① 首页首屏无「新任务」；阳性对照「新对话」确在', async ({ page }) => {
    await bootHome(page)
    // 断言「不存在」：`getByText` 是**文本**定位器（非 className），toHaveCount(0) 有效。
    await expect(page.getByText('新任务')).toHaveCount(0)
    // 阳性对照（真实替代事实）：侧栏第二项逐字「新对话」——证明定位机制有效，非恒真。
    await expect(page.getByTestId('sidebar-nav-new-chat')).toHaveText('新对话')
    expect(await page.locator('.sidebar').innerText()).not.toContain('新任务')
    // ⚠️ 不对 `chat-workspace` 做「整块不含『任务』」断言：欢迎语「…也可以调用企业
    // 技能和工具完成任务。」**合法**含「任务」二字（`ChatWorkspace.tsx::chat-welcome`
    // 逐字欢迎语），该断言会**恒假红**。本用例只在「新任务」这一**概念词**上钉零。
  })

  /* ----------------------------------------------------------------------- *
   * ② 诚实空态
   * ----------------------------------------------------------------------- */
  test('② 无数据：KPI 渲染「—」且无 0% / 0 次 假计数（正则页面文本断言）', async ({ page }) => {
    await bootHome(page, { hasData: false })
    // 第二屏默认折叠——展开后再断言。
    await page.getByTestId('home-second-screen').locator('summary').click()
    const kpi = page.locator('.kpi-row')
    await expect(kpi).toBeVisible()
    // 无数据 ⇒ 每个 KPI 值都是「—」（绝非 0）。
    await expect(kpi).toContainText('—')
    await expect(kpi).not.toContainText('0%')

    // 在**整页可见文本**上做假计数正则断言（不只测选择器存在）。
    const bodyText = await page.locator('body').innerText()
    // 阳性对照：该正则**能**命中假计数（证明断言非恒真）。
    expect('成功率 0%').toMatch(/\b0\s*%/)
    expect('执行了 0 次').toMatch(/\b0\s*次/)
    // 页面真实文本中**不得**出现假计数。
    expect(bodyText).not.toMatch(/\b0\s*%/)
    expect(bodyText).not.toMatch(/\b0\s*次/)
  })

  test('② docx 修改摘要：缺失字段渲染「—」而非 0，且产物 chip 计数不漂移', async ({ page }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([
        frame('run.final_answer', { text: REPLY, iteration: 1 }, 1),
        frame('run.completed', { status: 'completed' }, 2),
      ]),
      artifacts: [
        {
          id: DOCX_ID,
          kind: 'document_docx',
          title: '项目方案_正式版.docx',
          format: 'docx',
          content: '',
          content_ref: 'ref-content-1',
          source: 'document.edit',
          result_ref: 'ref-1',
          created_at: '2026-01-01T00:01:00Z',
          // `modified` / `added` 为**真实**计数；`removed` / `numeric_changes` **缺失**
          // ⇒ 必须渲染「—」，**不得** 0 兜底。
          diff: { modified: 3, added: 5 },
        },
      ],
    })
    await send(page, INTENT)

    const diff = page.getByTestId('chat-doc-diff')
    await expect(diff).toBeVisible()

    // 真实数字逐字。
    await expect(page.getByTestId('chat-doc-diff-modified')).toHaveText('修改 3 处')
    await expect(page.getByTestId('chat-doc-diff-added')).toHaveText('新增 5')
    // 缺失 ⇒ 「—」，**绝不** 0。
    const removed = page.getByTestId('chat-doc-diff-removed')
    await expect(removed).toHaveText('删除 —')
    await expect(removed).not.toContainText('0')
    const numeric = page.getByTestId('chat-doc-diff-numeric')
    await expect(numeric).toHaveText('数字变化 —')
    await expect(numeric).not.toContainText('0')

    // 产物 chip 计数不漂移：本 run 仅 1 条产物 ⇒ 恰好 1 个 chip。
    await expect(page.getByTestId('chat-artifact-chip')).toHaveCount(1)
    // 反证：首页聊天区**不**渲染 `/tasks` 工作台的 artifact-* 四件套。
    await expect(page.getByTestId('artifact-card')).toHaveCount(0)
  })

  /* ----------------------------------------------------------------------- *
   * ③ 零回归可见性
   * ----------------------------------------------------------------------- */
  test('③ 关键区域 testid 逐字存在且可见（首页 + 技能资产中心）', async ({ page }) => {
    await bootHome(page)
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
    await expect(page.getByTestId('chat-composer')).toBeVisible()

    // 同一进程内跳转技能资产中心（stub 持续生效）。
    await page.goto('/skills')
    await expect(page.getByTestId('skill-workspace')).toBeVisible()
    await expect(page.getByTestId('skill-library')).toBeVisible()
    await expect(page.getByTestId('skill-inspector')).toBeVisible()
  })

  /* ----------------------------------------------------------------------- *
   * ④ 产物四件套不漂移（照 inc32 口径）
   * ----------------------------------------------------------------------- */
  test('④a 有产物：artifact-card / -preview / -download 各恒为 1', async ({ page }) => {
    await bootRoute(page, '/tasks', {
      tasksRuns: [
        makeRun({
          run_id: 'run_inc43_art_1',
          intent: '产出报告的任务',
          artifacts: [
            {
              id: 'art_inc43_report_1',
              kind: 'report_markdown',
              title: '运行报告.md',
              format: 'markdown',
              content: '# 运行报告\n\n## 最终答案\n\n完成。\n',
              source: 'report.render',
              result_ref: 'ref-report-1',
              created_at: '2026-01-01T00:01:00Z',
            },
          ],
        }),
      ],
    })
    await expect(page.getByTestId('artifact-card')).toHaveCount(1)
    await expect(page.getByTestId('artifact-preview')).toHaveCount(1)
    await expect(page.getByTestId('artifact-download')).toHaveCount(1)
    // 有产物 ⇒ 诚实空态不出现。
    await expect(page.getByTestId('artifact-empty')).toHaveCount(0)
  })

  test('④b 无产物：artifact-empty 逐字「暂无生成结果」，且无产物卡', async ({ page }) => {
    await bootRoute(page, '/tasks', {
      tasksRuns: [makeRun({ run_id: 'run_inc43_empty_1', intent: '无产物的任务' })],
    })
    const empty = page.getByTestId('artifact-empty')
    await expect(empty).toHaveCount(1)
    await expect(empty).toHaveText('暂无生成结果')
    await expect(page.getByTestId('artifact-card')).toHaveCount(0)
  })
})
