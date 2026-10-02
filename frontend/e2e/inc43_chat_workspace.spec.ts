/**
 * INC43 / T01 —— 首页 Chat-first Agent Workspace 的**真实浏览器** e2e 钉子。
 *
 * 覆盖 P0-1 ~ P0-9 + 「首屏（聊天工作区 / 输入区）不含『任务』字样」：
 *   · P0-1 侧栏：「新任务」→「新对话」、「最近任务」→「最近对话」，且 5 个 `sidebar-nav-*`
 *     testid 逐字存在；`id/href/onSelect` 导航机制不变。
 *   · P0-2 Agent 头像行 `chat-agent-header`（`ForgeFlow Agent` + 副标题）。
 *   · P0-3 欢迎气泡 `chat-welcome`（逐字欢迎语）。
 *   · P0-4 用户气泡 `chat-user-message`（逐字原文）。
 *   · P0-5/P0-6 运行中轨迹：`chat-exec-trace` 默认 `open`、标题「正在处理你的诉求」、
 *     复制按钮 `chat-exec-copy`、进行中步 `data-state="active"`、`chat-exec-live`。
 *   · P0-7 完成后轨迹：默认收缩、摘要行 `chat-exec-trace-summary` 逐字
 *     「查看执行过程 · N 个步骤 · M 个工具 · K 个 Skill」，且 N/M/K 与 `GET /runs/{id}` 相等。
 *   · P0-8 产物 chip：`chat-artifact-chip` + 查看/下载/继续修改；下载 URL = 既有产物端点。
 *   · P0-9 富输入区：`chat-composer` + 5 子 testid；提交真发 `POST /workspace/tasks` 且**不跳页**。
 *
 * 网络边界（沿用 inc32/inc36/inc41 口径）：在 `/api/**` 边界 route 打桩，专注前端在给定
 * 后端响应下的真实交互；后端语义不在本 spec 范围。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const RUN_ID = 'run_inc43_chat_1'
const ARTIFACT_ID = 'art_inc43_docx_1'
const INTENT = '把方案改成正式版'
const REPLY = '已完成修改。'

type Cfg = {
  /** `GET /runs/{id}` 的 status。 */
  status: string
  /** `/events` 的 SSE 原始帧字符串；`null` ⇒ 返回 500（走轮询兜底 ⇒ 恒为「运行中」）。 */
  sseBody: string | null
  /** 该 run 的 steps（真实 payload）。 */
  steps: Record<string, unknown>[]
  /** 该 run 的 tool_invocations。 */
  tools: Record<string, unknown>[]
  /** 该 run 注入的 skills（`codeplane.injected.skills`）。 */
  skills: { id: string; version: string; name: string }[]
  /** 该 run 的 artifacts。 */
  artifacts: Record<string, unknown>[]
}

const cfg: Cfg = { status: 'running', sseBody: null, steps: [], tools: [], skills: [], artifacts: [] }

/** 采集到的 `POST /api/workspace/tasks` 请求体（用于断言提交真的发出）。 */
let lastCreateBody: Record<string, unknown> | null = null

function frame(type: string, data: Record<string, unknown>, seq: number) {
  return { run_id: RUN_ID, type, data, seq, ts: '2026-01-01T00:00:00Z' }
}

/** 把一组帧包成 SSE 文本（末帧 `[DONE]`，与后端收尾一致）。 */
function sse(frames: unknown[]): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'
}

async function stub(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    // 任务创建（异步句柄）——提交真发的就是它。
    if (path === '/api/workspace/tasks' && method === 'POST') {
      lastCreateBody = JSON.parse(req.postData() || '{}') as Record<string, unknown>
      return json({
        run_id: RUN_ID,
        thread_id: 'thread-1',
        status: 'running',
        detail: {},
        session_id: 'sess-1',
        parent_run_id: null,
      })
    }

    // 运行事件流：给定帧 / 500（触发 `useRunEvents` 的轮询兜底 ⇒ 保持「运行中」）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      if (cfg.sseBody == null) {
        return route.fulfill({ status: 500, contentType: 'text/plain', body: 'sse down' })
      }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: cfg.sseBody })
    }

    // 运行详情。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      return json({
        run_id: detailM[1],
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

    // 运行列表 / 会话列表（首页「近期任务」rail + ModelStatus 数据源）——诚实空。
    if ((path === '/api/runs' || path === '/api/workspace/sessions') && method === 'GET') {
      return json({ total: 0, items: [] })
    }
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/skills') return json({ total: 0, items: [] })

    // 其余一律诚实空（`{items:[]}` 形状，避免无关 4xx / undefined 崩溃）。
    return json({ total: 0, items: [] })
  })
}

async function signIn(page: Page) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
    // admin：`execute:workflows` ⇒ 可提交；且其建议 chips 不含「任务」字样。
    window.sessionStorage.setItem('forgeflow.role', 'admin')
    window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
  })
}

/** 打开首页（默认无 run：仅欢迎态）。 */
async function bootHome(page: Page, c: Partial<Cfg> = {}) {
  Object.assign(cfg, { status: 'running', sseBody: null, steps: [], tools: [], skills: [], artifacts: [] }, c)
  lastCreateBody = null
  await signIn(page)
  await stub(page)
  await page.goto('/')
  await expect(page.getByTestId('chat-workspace')).toBeVisible()
}

/** 在富输入区提交一条消息。 */
async function send(page: Page, text: string) {
  await page.getByTestId('chat-composer-input').fill(text)
  await page.getByTestId('chat-composer-send').click()
}

test.describe('INC43 首页 Chat-first Workspace', () => {
  test('P0-1 侧栏：无「新任务」，第二/三项为「新对话 / 最近对话」且 sidebar-nav-* 逐字存在', async ({
    page,
  }) => {
    await bootHome(page)
    const sidebar = page.locator('.sidebar')
    await expect(page.getByTestId('sidebar-nav-workspace')).toHaveText('工作区')
    await expect(page.getByTestId('sidebar-nav-new-chat')).toHaveText('新对话')
    await expect(page.getByTestId('sidebar-nav-recent-chat')).toHaveText('最近对话')
    await expect(page.getByTestId('sidebar-nav-skills')).toHaveText('技能')
    await expect(page.getByTestId('sidebar-nav-knowledge')).toHaveText('知识库')
    // 「新任务」字样彻底消失（含子串）。
    expect(await sidebar.innerText()).not.toContain('新任务')
    // 导航机制不变：仍指向既有 `#tasks`（`onSelect('tasks')` 承载）。
    await expect(page.getByTestId('sidebar-nav-new-chat')).toHaveAttribute('href', '#tasks')
    // 既有 `/tasks` 路由仍可达。
    await page.goto('/tasks')
    await expect(page).toHaveURL(/\/tasks$/)
  })

  test('P0-2/P0-3 首屏：Agent 头像行 + 欢迎气泡逐字，且聊天工作区不含「新任务」', async ({
    page,
  }) => {
    await bootHome(page)
    const header = page.getByTestId('chat-agent-header')
    await expect(header).toBeVisible()
    await expect(header).toContainText('ForgeFlow Agent')
    await expect(header).toContainText('随时告诉我你想完成什么')
    await expect(page.getByTestId('chat-welcome')).toHaveText(
      '你好，我可以帮你分析、编写、修改、生成文件，也可以调用企业技能和工具完成任务。',
    )
    await expect(page.getByTestId('chat-thread')).toBeVisible()
    // 首屏（聊天工作区 + 输入区）不含「新任务」这一**概念词**（PRD P0-1）。
    // ⚠️ 不得放宽成「不含任何『任务』子串」：欢迎语「…也可以调用企业技能和工具
    // **完成任务**。」本身**合法**含「任务」二字 —— 那样断言会恒假红（原缺陷）。
    // 故只在「新任务」/「新建任务」这两个概念词上钉零。
    const workspaceText = await page.getByTestId('chat-workspace').innerText()
    const composerText = await page.getByTestId('chat-composer').innerText()
    // 阳性对照：工作区文本**非空且确含**「完成任务」，证明下一条断言**不是空过**。
    expect(workspaceText).toContain('完成任务')
    expect(workspaceText).not.toContain('新任务')
    expect(workspaceText).not.toContain('新建任务')
    expect(composerText).not.toContain('新任务')
    expect(composerText).not.toContain('新建任务')
  })

  test('P0-4/P0-5/P0-6 运行中：用户原文逐字 + 轨迹默认展开 + 进行中步 + live 行', async ({
    page,
  }) => {
    await bootHome(page, {
      status: 'running',
      // 运行中：SSE 返回 500 ⇒ 轮询兜底 ⇒ 恒为「运行中」（不会误触发终态收缩）。
      sseBody: null,
      steps: [
        { index: 0, tool: 'docs.parse', note: '读取文档', status: 'ok' },
        { index: 1, tool: 'report.render', note: '生成修改后的文档', status: 'running' },
      ],
      tools: [
        { tool: 'docs.parse', status: 'ok', executed: true, latency_ms: 0.4, step_id: `${RUN_ID}:1:0` },
        { tool: 'report.render', status: 'running', executed: false, latency_ms: null, step_id: `${RUN_ID}:1:1` },
      ],
    })
    await send(page, INTENT)

    await expect(page.getByTestId('chat-user-message')).toHaveText(INTENT)
    const trace = page.getByTestId('chat-exec-trace')
    await expect(trace).toBeVisible()
    // 默认展开。
    expect(await trace.evaluate((el) => (el as HTMLDetailsElement).open)).toBe(true)
    await expect(page.getByTestId('chat-exec-trace-summary')).toContainText('正在处理你的诉求')
    await expect(page.getByTestId('chat-exec-copy')).toBeVisible()
    await expect(page.getByTestId('chat-exec-live')).toHaveText('正在生成……')
    await expect(page.getByTestId('chat-exec-trace-step').first()).toBeVisible()
    await expect(trace.locator('[data-state="active"]')).toHaveCount(1)
  })

  test('P0-7/P0-8 已完成：轨迹自动收缩且 N/M/K 可复算 + 产物 chip 三动作', async ({ page }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([
        frame('run.final_answer', { text: REPLY, iteration: 1 }, 1),
        frame('run.completed', { status: 'completed' }, 2),
      ]),
      // 4 个步骤（N=4）。
      steps: [
        { index: 0, tool: 'docs.parse', note: '读取文档', status: 'ok' },
        { index: 1, tool: 'skill.invoke', note: '调用文档重写技能', status: 'ok' },
        { index: 2, tool: 'analyse.text', note: '修改内容', status: 'ok' },
        { index: 3, tool: 'export.docx', note: '生成新版本', status: 'ok' },
      ],
      // 2 个工具（去重后的 tool_invocations.tool）。
      tools: [
        { tool: 'analyse.text', status: 'ok', executed: true, latency_ms: 0.5, step_id: `${RUN_ID}:1:2` },
        { tool: 'export.docx', status: 'ok', executed: true, latency_ms: 0.3, step_id: `${RUN_ID}:1:3` },
      ],
      // 1 个 Skill。
      skills: [{ id: 'skill_doc_rewrite', version: '2.1', name: '文档重写' }],
      artifacts: [
        {
          id: ARTIFACT_ID,
          kind: 'document_docx',
          title: '项目方案_正式版.docx',
          format: 'docx',
          content: '',
          source: 'document.edit',
          result_ref: 'ref-1',
          created_at: '2026-01-01T00:01:00Z',
        },
      ],
    })
    await send(page, INTENT)

    // 完成后自动收缩为一行。
    const trace = page.getByTestId('chat-exec-trace')
    await expect(trace).toBeVisible()
    await expect
      .poll(async () => trace.evaluate((el) => (el as HTMLDetailsElement).open))
      .toBe(false)

    // 摘要行逐字 + N/M/K 与 `GET /runs/{id}` 逐一相等（4 步骤 · 2 工具 · 1 Skill）。
    await expect(page.getByTestId('chat-exec-trace-summary')).toContainText(
      '查看执行过程 · 4 个步骤 · 2 个工具 · 1 个 Skill',
    )

    // Agent 回复正文。
    await expect(page.getByTestId('chat-agent-reply')).toHaveText(REPLY)

    // 产物 chip：文件名 + 查看/下载/继续修改；下载 URL = 既有产物端点。
    const chip = page.getByTestId('chat-artifact-chip')
    await expect(chip).toHaveCount(1)
    await expect(chip).toContainText('项目方案_正式版.docx')
    await expect(page.getByTestId('chat-artifact-view')).toBeVisible()
    await expect(page.getByTestId('chat-artifact-continue')).toBeVisible()
    await expect(page.getByTestId('chat-artifact-download')).toHaveAttribute(
      'data-download-url',
      `/api/runs/${RUN_ID}/artifacts/${ARTIFACT_ID}`,
    )

    // 点击摘要行可展开回顾：4 步 + 1 个「调用「X」Skill」条目 = 5。
    // 点左上角（caret 处），避免落到右侧「复制」按钮上。
    await page.getByTestId('chat-exec-trace-summary').click({ position: { x: 10, y: 10 } })
    await expect
      .poll(async () => trace.evaluate((el) => (el as HTMLDetailsElement).open))
      .toBe(true)
    await expect(page.getByTestId('chat-exec-trace-step')).toHaveCount(5)
    await expect(trace).toContainText('调用「文档重写」Skill')
  })

  test('P0-9 富输入区：chat-composer 五子 testid 齐全，提交真发 POST /workspace/tasks 且不跳页', async ({
    page,
  }) => {
    await bootHome(page, { status: 'running', sseBody: null })
    await expect(page.getByTestId('chat-composer')).toBeVisible()
    await expect(page.getByTestId('chat-composer-attach')).toBeVisible()
    await expect(page.getByTestId('chat-composer-skill')).toBeVisible()
    await expect(page.getByTestId('chat-composer-tools')).toBeVisible()
    await expect(page.getByTestId('chat-composer-input')).toBeVisible()
    await expect(page.getByTestId('chat-composer-send')).toBeVisible()

    // 旧附件的字面量 testid 保留（同批控件承载）。
    await expect(page.getByTestId('hero-attach-files')).toHaveCount(1)
    await expect(page.getByTestId('hero-attach-skills')).toHaveCount(1)
    await expect(page.getByTestId('hero-attach-more')).toHaveCount(1)

    // 「添加文件」打开既有资源选择器面板。
    await page.getByTestId('chat-composer-attach').click()
    await expect(page.getByTestId('hero-attach-panel')).toBeVisible()
    await expect(page.getByTestId('resource-add')).toBeVisible()

    await send(page, INTENT)
    await expect.poll(() => lastCreateBody?.intent).toBe(INTENT)
    expect(lastCreateBody?.workflow_type).toBe('generic')
    // 不跳页：URL 仍为 `/`。
    expect(new URL(page.url()).pathname).toBe('/')
    await expect(page.getByTestId('chat-user-message')).toHaveText(INTENT)
  })

  test('P0-14 保留契约：home-second-screen / model-status / 内联会话 rail 仍在首页', async ({
    page,
  }) => {
    await bootHome(page)
    await expect(page.getByTestId('home-second-screen')).toHaveCount(1)
    await expect(page.getByTestId('model-status')).toHaveCount(1)
    // 近期任务 rail（历史行）仍在（其点击行为由 inc41 专测；此处仅确认契约未丢）。
    //
    // ⚠️ 反空过（修弱证明力）：空 seed（0 run）下 `conv-inline-history-row` **恒为 0**，
    // 若只留 `toHaveCount(0)`，则无论 rail 是否被删都恒绿 —— 该断言可被「删掉整个 rail」
    // 而**不受影响**（弱证明力，会被 QA 判为「空过」）。
    // 故补**阳性对照**：先用用户可见事实（栏目标题「近期任务」）钉住 rail 容器真实存在且可见，
    // 再断言其为空（0 行）。反事实：删掉 `RecentTasks` 渲染 ⇒ 标题随之消失 ⇒ 本用例必红。
    const rail = page.locator('.rail-card').filter({ hasText: '近期任务' })
    await expect(rail).toBeVisible()
    await expect(rail).toContainText('近期任务')
    await expect(page.getByTestId('conv-inline-history-row')).toHaveCount(0)
  })
})
