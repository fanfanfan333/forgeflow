/**
 * INC33 / R1 — INC32 工作台（`/tasks` 会话工作台）**真实浏览器** e2e。
 *
 * 目标：在**真实构建产物**（`vite preview`）上用真实浏览器驱动 INC32 的三栏工作台，
 * 机械判定「真点击 → 真请求 → 真 UI 变化」，为 INC32 新增的按钮/面板补齐此前**零覆盖**
 * 的证据缺口（`e2e/` 原有 3 份 spec 全部打桩在 `/api/**` 边界、无一触碰工作台）：
 *
 *   1. 三栏骨架就位（`workspace-columns` / `-col-history` / `-col-conversation` / `-col-artifacts`）；
 *   2. 派发**真的**发出 `POST /api/workspace/tasks`，且返回句柄驱动中列标题**逐字**上屏；
 *   3. 停止**真的**发出 `POST /api/runs/{id}/abort` ⇒ 徽标逐字「已中止」且按钮消失；
 *   4. 负向对照（防装饰）：非运行中 / 只读身份 ⇒ `workspace-stop` **不存在**；
 *   5. 产物：诚实空态 `artifact-empty` 逐字「暂无生成结果」；有产物 ⇒ `artifact-card`=1、
 *      `artifact-preview` **逐字**、下载**真的**拉取内容并生成正确的下载锚点。
 *
 * ── 网络边界（分层诚实，沿用 inc29/inc26 spec 的既有口径）────────────────────────
 * 本 spec 在 `/api/**` 边界做 route 拦截（stub 后端），专注验证**前端**在给定后端响应下的
 * 真实交互。后端语义（异步派发到底怎么跑、abort 的 409/404/403、artifacts 的真实字节）
 * **不在**本 spec 范围 —— 那些由后端集成测试覆盖。
 *
 * ── ⚠️ 与验收口径不符之处（如实声明，绝不为了绿而弱化断言）────────────────────────
 * 验收清单第 5 条写「`artifact-download` 的 `href`/`download` 属性正确」。但 INC32 的
 * **实际实现**里 `artifact-download` 是一个 `<button>`（`ArtifactPanel.tsx::ArtifactCard`），
 * **没有** `href`/`download` 属性：产物端点位于 JWT/Bearer 网关之后，裸 `<a download>`
 * 会以**未认证**身份到达（见 `client.ts::downloadArtifact` 顶部注释，ADR-05）。真实机制是
 * 「按钮 → 认证 `fetch` 取回字节 → `Blob` → 临时 `<a>`（`href=blob:`、`download=文件名`）
 * → 程序化 `click()`」。因此本 spec 断言的是**真实机制**：
 *   · 点击 `artifact-download` **真的**发出 `GET /api/runs/{id}/artifacts/{aid}`；
 *   · 该次下载生成的临时锚点 `download` 逐字 = 产物文件名、`href` 是 `blob:`（证明来自真实取回）。
 * 若强行断言「`artifact-download` 自带 href/download」会得到**假红**（前提错误）。
 *
 * 引文纪律：一律 `文件名::符号名`。所有 testid 均为 INC32 新增（见 `_qa_inc32/testid_audit.txt`）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

declare global {
  interface Window {
    /** 本 spec 记录「程序化下载锚点」的钩子（见 boot 的 addInitScript）。 */
    __ffDownloads?: { href: string | null; download: string | null }[]
  }
}

// --- 夹具 ------------------------------------------------------------------ //

/** 派发用的**唯一**意图串（逐字断言中列标题用）。 */
const INTENT = '待验证：整理 Acme 销售线索（INC32 e2e）'

/** 产物正文（逐字），刻意含 `##` 字面，验证 `<pre>` 不过 Markdown 渲染。 */
const ARTIFACT_CONTENT =
  '# 运行报告\n\n**意图**：整理 Acme 销售线索\n\n## 最终答案\n\n- 线索 A 有效\n- 线索 B 待跟进\n\n## 一、执行记录\n\n（略）\n'

type Artifact = {
  id: string
  kind: string
  title: string
  format: string
  content: string
  source: string
  result_ref: string
  created_at: string
}

const ARTIFACT: Artifact = {
  id: 'art_inc32_1',
  kind: 'report_markdown',
  title: '运行报告.md',
  format: 'markdown',
  content: ARTIFACT_CONTENT,
  source: 'report.render',
  result_ref: 'deadbeefdeadbeef',
  created_at: '2026-01-01T00:00:10Z',
}

/** `GET /runs/{id}` 的详情形状（只保留本 spec 会读到的字段）。 */
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
  steps: { index: number; status: string; tool?: string }[]
  errors: string[]
  artifacts: Artifact[]
  session_id: string
  parent_run_id: string | null
}

/** `POST /workspace/tasks` 请求体（客户端形状，逐字对照 `client.ts::workspaceCreateTask`）。 */
type TaskBody = {
  intent?: string
  workflow_type?: string
  context?: { table?: string; paths?: string[]; resources?: string[] }
  session_id?: string
  parent_run_id?: string
}

type Rec = {
  workspaceTasks: { method: string; path: string; body: TaskBody }[]
  aborts: { method: string; path: string }[]
  artifactGets: string[]
}

const rec: Rec = { workspaceTasks: [], aborts: [], artifactGets: [] }

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
    artifacts: over.artifacts ?? [],
    session_id: over.session_id ?? '',
    parent_run_id: over.parent_run_id ?? null,
  }
}

/** `RunDetail` → `RunSummary`（列表项形状）。 */
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

// --- 桩后端 ----------------------------------------------------------------- //

async function stubApi(page: Page, seed: RunLike[]) {
  const db: { order: string[]; detail: Record<string, RunLike> } = {
    order: seed.map((r) => r.run_id),
    detail: Object.fromEntries(seed.map((r) => [r.run_id, r])),
  }

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 派发（异步创建）—— 捕获 method/path/body，并让句柄可被选中。
    if (path === '/api/workspace/tasks' && method === 'POST') {
      const body = req.postDataJSON() as TaskBody
      rec.workspaceTasks.push({ method, path, body })
      const id = `run_new_${db.order.length + 1}`
      const created = makeRun({
        run_id: id,
        status: 'running',
        outcome: '',
        intent: body.intent ?? '',
        completed_at: null,
        session_id: body.session_id ?? '',
        parent_run_id: body.parent_run_id ?? null,
      })
      db.detail[id] = created
      db.order.unshift(id)
      return json({
        run_id: id,
        thread_id: created.thread_id,
        status: created.status,
        detail: {},
        session_id: created.session_id,
        parent_run_id: created.parent_run_id,
      })
    }

    // 停止（终态、不可逆）。
    const abortM = /^\/api\/runs\/([^/]+)\/abort$/.exec(path)
    if (abortM && method === 'POST') {
      rec.aborts.push({ method, path })
      const d = db.detail[abortM[1]]
      if (d) {
        d.status = 'aborted'
        d.outcome = 'aborted'
        d.completed_at = '2026-01-01T00:03:00Z'
      }
      return json({ run_id: abortM[1], status: 'aborted' })
    }

    // 产物原文（认证 fetch 的目标）。
    const artM = /^\/api\/runs\/([^/]+)\/artifacts\/([^/]+)$/.exec(path)
    if (artM && method === 'GET') {
      rec.artifactGets.push(path)
      return route.fulfill({ status: 200, contentType: 'text/markdown', body: ARTIFACT_CONTENT })
    }

    // 运行事件流（SSE）：给空流，让实时条立即收尾（本 spec 不验证实时流）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }

    // 运行详情。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      const d = db.detail[detailM[1]]
      if (!d) return json({ detail: 'not found' }, 404)
      return json(d)
    }

    // 运行列表（左列数据源）。
    if (path === '/api/runs' && method === 'GET') {
      return json({ total: db.order.length, items: db.order.map((id) => summaryOf(db.detail[id])) })
    }

    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.csv', '.txt', '.md'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json({ total: 0, items: [] })

    // 其余（session / 其它面板）一律空对象，避免无关 4xx 噪声。
    return json({})
  })
}

/**
 * 统一开屏：设置会话身份 + 安装下载锚点钩子 + 打桩 + 打开工作台。
 *
 * 会话身份（决定「可执行」与否，AC-34/AC-37）：`LiveRunsView` 用 `useSession()` +
 * `roleConfigFor(role).canExecute` 决定是否渲染停止按钮。`getSession()` 需要
 * `sessionStorage['forgeflow.jwt']` 存在才返回会话，故此处显式注入。
 */
async function boot(page: Page, opts: { role?: string; runs?: RunLike[] } = {}) {
  rec.workspaceTasks = []
  rec.aborts = []
  rec.artifactGets = []

  await page.addInitScript(
    (role: string) => {
      window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
      window.sessionStorage.setItem('forgeflow.role', role)
      window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
      // 记录**程序化**下载锚点（真实下载意图的可观测证据）。
      // 只拦截含 download 属性或 blob: href 的锚点，绝不影响页面导航。
      window.__ffDownloads = []
      const orig = HTMLAnchorElement.prototype.click
      HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
        const href = this.getAttribute('href')
        const download = this.getAttribute('download')
        if (download != null || (href ?? '').startsWith('blob:')) {
          const arr = window.__ffDownloads ?? (window.__ffDownloads = [])
          arr.push({ href, download })
          return
        }
        return orig.call(this)
      }
    },
    opts.role ?? 'admin',
  )

  await stubApi(page, opts.runs ?? [])
  await page.goto('/tasks')
}

// --- 共享夹具 --------------------------------------------------------------- //

const RUNNING = makeRun({
  run_id: 'run_live_1',
  status: 'running',
  outcome: '',
  intent: '演示运行中的任务',
  completed_at: null,
  steps: [{ index: 0, status: 'running', tool: 'research.search' }],
})

const COMPLETED = makeRun({
  run_id: 'run_done_1',
  status: 'completed',
  outcome: 'success',
  intent: '已完成的任务',
  completed_at: '2026-01-01T00:01:00Z',
})

const WITH_ARTIFACT = makeRun({
  run_id: 'run_art_1',
  status: 'completed',
  outcome: 'success',
  intent: '产出报告的任务',
  completed_at: '2026-01-01T00:01:00Z',
  artifacts: [ARTIFACT],
})

// --- 用例 ------------------------------------------------------------------- //

test.describe('INC32 工作台 E2E', () => {
  // ① 三栏骨架就位。
  test('三栏骨架就位：四列 testid 各 1 且可见', async ({ page }) => {
    await boot(page, { runs: [] })
    await expect(page.getByTestId('workspace-columns')).toHaveCount(1)
    await expect(page.getByTestId('workspace-columns')).toBeVisible()
    await expect(page.getByTestId('workspace-col-history')).toHaveCount(1)
    await expect(page.getByTestId('workspace-col-history')).toBeVisible()
    await expect(page.getByTestId('workspace-col-conversation')).toHaveCount(1)
    await expect(page.getByTestId('workspace-col-conversation')).toBeVisible()
    await expect(page.getByTestId('workspace-col-artifacts')).toHaveCount(1)
    await expect(page.getByTestId('workspace-col-artifacts')).toBeVisible()
  })

  // ② 派发真的发出 POST /api/workspace/tasks，且句柄驱动中列标题逐字。
  test('派发真发请求：POST /api/workspace/tasks 且句柄驱动中列标题逐字', async ({ page }) => {
    await boot(page, { runs: [] })
    await page.getByLabel('新任务描述').fill(INTENT)
    await page.getByTestId('run-declare-table').fill('public.orders')
    await page.getByTestId('run-declare-paths').fill('a.py, b.py')
    await page.getByRole('button', { name: '运行任务' }).click()

    await expect.poll(() => rec.workspaceTasks.length).toBe(1)
    const call = rec.workspaceTasks[0]
    expect(call.method).toBe('POST')
    expect(call.path).toBe('/api/workspace/tasks')
    expect(call.body.intent).toBe(INTENT)
    expect(call.body.context?.table).toBe('public.orders')
    expect(call.body.context?.paths).toEqual(['a.py', 'b.py'])

    // 返回句柄驱动中列标题逐字（`RunHeader` <h1> 取 `real.intent`）。
    await expect(page.locator('.runs-head h1')).toHaveText(INTENT)
  })

  // ③ 停止真点击 ⇒ POST abort ⇒ 徽标「已中止」且按钮消失。
  test('停止真点击：POST abort → 徽标逐字「已中止」且停止按钮消失', async ({ page }) => {
    await boot(page, { runs: [RUNNING] })
    const stop = page.getByTestId('workspace-stop')
    await expect(stop).toHaveCount(1)
    await expect(stop).toHaveText('停止任务')

    await stop.click()
    await expect.poll(() => rec.aborts.length).toBe(1)
    expect(rec.aborts[0].method).toBe('POST')
    expect(rec.aborts[0].path).toBe(`/api/runs/${RUNNING.run_id}/abort`)

    // 详情改回 aborted ⇒ 徽标逐字「已中止」。
    await expect(page.getByTestId('result-delivery')).toHaveText('已中止')
    // 且 run 变终态 ⇒ 停止按钮消失（count 0）。
    await expect(page.getByTestId('workspace-stop')).toHaveCount(0)
  })

  // ④a 负向对照：非运行中不渲染停止按钮。
  test('负向对照：非运行中不渲染停止按钮', async ({ page }) => {
    await boot(page, { runs: [COMPLETED] })
    // 中列确实渲染了该 run（标题逐字上屏）—— 证明「无按钮」是该 run 非运行中所致。
    await expect(page.locator('.runs-head h1')).toHaveText(COMPLETED.intent)
    await expect(page.getByTestId('workspace-stop')).toHaveCount(0)
  })

  // ④b 负向对照：只读身份（viewer）即便 run 运行中也不渲染停止按钮（AC-34/AC-37）。
  test('负向对照：只读身份运行中也不渲染停止按钮', async ({ page }) => {
    await boot(page, { runs: [RUNNING], role: 'viewer' })
    // run 确实是运行中：实时条挂载（仅运行中渲染）—— 证明「无按钮」是身份受限所致。
    await expect(page.getByTestId('workspace-live-strip')).toHaveCount(1)
    await expect(page.getByTestId('workspace-stop')).toHaveCount(0)
  })

  // ⑤a 产物诚实空态。
  test('产物诚实空态：artifact-empty 逐字「暂无生成结果」', async ({ page }) => {
    await boot(page, { runs: [COMPLETED] })
    const empty = page.getByTestId('artifact-empty')
    await expect(empty).toHaveCount(1)
    await expect(empty).toHaveText('暂无生成结果')
    // 空态下不渲染任何产物卡（绝不占位）。
    await expect(page.getByTestId('artifact-card')).toHaveCount(0)
  })

  // ⑤b 产物卡 + 逐字预览 + 真实下载。
  test('产物卡与下载：card=1 + preview 逐字 + download 真拉取且锚点正确', async ({ page }) => {
    await boot(page, { runs: [WITH_ARTIFACT] })

    await expect(page.getByTestId('artifact-card')).toHaveCount(1)

    const preview = page.getByTestId('artifact-preview')
    await expect(preview).toBeVisible()
    // 逐字（textContent 不归一化空白；`toHaveText` 会折叠换行故不用）。
    expect(await preview.textContent()).toBe(ARTIFACT_CONTENT)

    const dl = page.getByTestId('artifact-download')
    await expect(dl).toHaveCount(1)
    await expect(dl).toHaveText('下载')

    await dl.click()
    // 真实拉取：GET /api/runs/{id}/artifacts/{aid}。
    await expect.poll(() => rec.artifactGets.length).toBe(1)
    expect(rec.artifactGets[0]).toBe(`/api/runs/${WITH_ARTIFACT.run_id}/artifacts/${ARTIFACT.id}`)

    // 程序化下载锚点：href=blob:（真实取回）且 download=文件名（逐字）。
    // 注意：__ffDownloads 是取回完成、锚点创建后才追加的 —— 与上面 artifactGets
    // 之间隔着一拍，并发全量下曾出现约 1/6 的假红（click 后立即读为 0）。
    // 计数断言必须走 expect.poll（带重试），逐字断言在计数到位后再做。
    await expect
      .poll(() => page.evaluate(() => (window.__ffDownloads ?? []).length))
      .toBe(1)
    const downloads = await page.evaluate(() => window.__ffDownloads ?? [])
    expect(downloads[0].download).toBe(ARTIFACT.title)
    expect((downloads[0].href ?? '').startsWith('blob:')).toBe(true)
  })

  // ⑥ 计划 (b) —— run 终态必须出中文标签，**不得漏英文**。
  // 裁断口径：只测 `interrupted` / `rejected` 这两个 **run 终态**；`refused` 是工具
  // 调用 / 步骤状态（见 `orchestrator.py` 的 ("error","unavailable","refused")），
  // 不作 run 状态来测。
  test('状态标签不漏英文：interrupted→「已中断」/ rejected→「已拒绝」逐字且无 ASCII 字母', async ({
    page,
  }) => {
    const terminals = [
      { status: 'interrupted', outcome: 'interrupted', label: '已中断' },
      { status: 'rejected', outcome: 'rejected', label: '已拒绝' },
    ]
    for (const t of terminals) {
      await boot(page, {
        runs: [
          makeRun({
            run_id: `run_${t.status}_1`,
            status: t.status,
            outcome: t.outcome,
            intent: `${t.status} 终态`,
            completed_at: '2026-01-01T00:01:00Z',
          }),
        ],
      })
      // RunHeader 徽标（`realRun.ts::runStatusMeta`）—— 无 testid，按 class 定位。
      const headerBadge = page.locator('.runs-head .badge')
      await expect(headerBadge).toHaveText(t.label)
      // 结果层交付徽标（`realRun.ts::deliveryState`，testid `result-delivery`）。
      const delivery = page.getByTestId('result-delivery')
      await expect(delivery).toHaveText(t.label)

      // 机械判据：渲染文案**逐字等于**目标中文，且**不含任何 ASCII 字母**（漏英文即失败）。
      const headText = ((await headerBadge.textContent()) ?? '').trim()
      const deliveryText = ((await delivery.textContent()) ?? '').trim()
      expect({ text: headText, hasAscii: /[A-Za-z]/.test(headText) }).toEqual({
        text: t.label,
        hasAscii: false,
      })
      expect({ text: deliveryText, hasAscii: /[A-Za-z]/.test(deliveryText) }).toEqual({
        text: t.label,
        hasAscii: false,
      })
    }
  })
})
