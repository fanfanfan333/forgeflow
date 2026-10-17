/**
 * AgentFlow 2.0 / Phase B —— 三栏「Agent 工作台」端到端自证（T-B05）。
 *
 * 目标：在**真实浏览器**里证明 Phase B 的验收口径 —— 用户「从一次任务出发」，
 * 在**同一上下文**里同时看到：
 *   · 左栏 = 上下文 / 对象树（Task → 文件·数据 → Skill）；
 *   · 中栏 = Agent 工作流 · 对话 · 结果（既有 `chat-workspace`）；
 *   · 右栏 = 对象 Inspector（点击左栏对象 / 中栏产物 ⇒ 右栏切换详情）。
 * 另证明窄屏（390px）折叠为**单列且无横向溢出**。
 *
 * 网络边界（沿用 inc32/inc36/inc41/inc43 口径）：在 `/api/**` 边界 route 打桩，专注
 * 前端在给定后端响应下的真实交互；后端语义不在本 spec 范围。**不改**任何既有
 * `data-testid`（新增断言只读既有 testid + Phase B 新 testid）。
 *
 * ⚠️ 诚实声明（T-B06 —— 修正「自证掩盖真相」）：本 spec 的 `/api/**` **全是桩**。
 * 其中 `codeplane.injected` 是**桩输入**，只用来证明一条**前端契约**：
 * 「若 `GET /runs/{id}` 真的返回了 `codeplane.injected`，左栏「本次涉及 · Skill/记忆」
 * 会**如实渲染**它」。它**不**证明、也**不**声称**后端能产出** `injected` —— 后端该能力
 * 由**真链路**单测 `tests/unit/test_tb06_codeplane_injected_survives.py`（handler →
 * `_assemble_codeplane` → `RunDetailResponse`）与**真后端冒烟**独立证明。
 * 作为对照，第三例（阴性对照）把桩改成**不带** `injected` 的真实（修复前）后端形状，
 * 断言左栏 Skill 组回落到诚实空态「本次未调用技能」——证明前端**绝不臆造** Skill。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const SESSION_ID = 'sess_phase_b_1'
const RUN_ID = 'run_phase_b_1'
const ARTIFACT_ID = 'art_phase_b_1'
const INTENT = '把方案改成正式版'
const FILENAME = '项目方案_正式版.docx'
const REPLY = '已完成修改，生成正式版文档。'

function frame(type: string, data: Record<string, unknown>, seq: number) {
  return { run_id: RUN_ID, type, data, seq, ts: '2026-01-01T00:00:00Z' }
}

/** 把一组帧包成 SSE 文本（末帧 `[DONE]`，与后端收尾一致）。 */
function sse(frames: unknown[]): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n'
}

/**
 * 运行详情（`GET /runs/{id}`）—— completed，带 steps / tools / skills / artifacts。
 *
 * `withInjected`（T-B06）控制 `codeplane` 的形状：
 *   · `true`  —— **桩输入**：注入一段 `codeplane.injected`（skills 含「文档重写」），
 *                仅用于驱动前端契约（左栏如实渲染后端给的 injected）；
 *   · `false` —— 后端**修复前**的真实形状：`codeplane` 无 `injected` 键（该键曾被
 *                `_assemble_codeplane` 丢弃）。用于阴性对照，证明左栏不臆造 Skill。
 */
function runDetailBody(withInjected = true): Record<string, unknown> {
  return {
    run_id: RUN_ID,
    thread_id: 'thread-1',
    status: 'completed',
    outcome: 'success',
    intent: INTENT,
    steps: [
      { index: 0, tool: 'docs.parse', note: '读取文档', status: 'ok' },
      { index: 1, tool: 'export.docx', note: '生成新版本', status: 'ok' },
    ],
    errors: [],
    created_at: '2026-01-01T00:00:00Z',
    completed_at: '2026-01-01T00:01:00Z',
    experience_id: null,
    runtime_mode: 'llm',
    tool_invocations: [
      { tool: 'docs.parse', status: 'ok', executed: true, latency_ms: 0.4, step_id: `${RUN_ID}:1:0` },
      { tool: 'export.docx', status: 'ok', executed: true, latency_ms: 0.3, step_id: `${RUN_ID}:1:1` },
    ],
    // ⚠️ 桩输入（非后端能力声明）：见文件头「诚实声明」。
    codeplane: withInjected
      ? { injected: { skills: [{ id: 'skill_doc_rewrite', version: '2.1', name: '文档重写' }], memory: [] } }
      : {},
    artifacts: [
      {
        id: ARTIFACT_ID,
        kind: 'document_docx',
        title: FILENAME,
        format: 'docx',
        content: '',
        source: 'document.edit',
        result_ref: 'ref-1',
        created_at: '2026-01-01T00:01:00Z',
      },
    ],
    session_id: SESSION_ID,
    parent_run_id: null,
  }
}

/** 会话详情（`GET /workspace/sessions/{id}`）—— 一条已完成 run。 */
function sessionDetailBody(): Record<string, unknown> {
  return {
    session_id: SESSION_ID,
    title: INTENT,
    runs: [
      {
        run_id: RUN_ID,
        thread_id: 'thread-1',
        status: 'completed',
        outcome: 'success',
        intent: INTENT,
        title: INTENT,
        created_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:01:00Z',
        experience_id: null,
        session_id: SESSION_ID,
        parent_run_id: null,
      },
    ],
  }
}

async function stub(page: Page, withInjected = true) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const json = (b: unknown, s = 200) =>
      route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) })

    // 运行事件流：completed 收尾（让 Agent turn 收敛为一行摘要 + 正文 + 产物 chip）。
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([
          frame('run.final_answer', { text: REPLY, iteration: 1 }, 1),
          frame('run.completed', { status: 'completed' }, 2),
        ]),
      })
    }

    // 单个会话（左栏对象树数据源）—— 必须先于 `/workspace/sessions` 列表匹配。
    const sessionM = /^\/api\/workspace\/sessions\/([^/]+)$/.exec(path)
    if (sessionM && method === 'GET') {
      return json(sessionDetailBody())
    }

    // 运行详情（中栏回复兜底 + 左栏对象派生 + 右栏事实源）。
    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      return json(runDetailBody(withInjected))
    }

    // 会话列表 / 运行列表（近期任务 rail + ModelStatus 数据源）—— 诚实空。
    if ((path === '/api/runs' || path === '/api/workspace/sessions') && method === 'GET') {
      return json({ total: 0, items: [] })
    }
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/skills') return json({ total: 0, items: [] })
    if (path === '/api/skills/resolve') return json({
      id: 'skill_doc_rewrite', tenant_id: 'e2e', name: '文档重写', domain: 'documents',
      owner: null, description: '按指令重写文档', current_version: '2.1', status: 'published',
      usage_count: 1, featured: false, tags: [], created_at: '2026-01-01', updated_at: '2026-01-01',
    })
    if (path === '/api/skills/skill_doc_rewrite/readiness') return json({
      skill_id: 'skill_doc_rewrite', tenant_id: 'e2e', ready: false, evaluated: 0,
      rate: null, has_version: true, current_version: '2.1', checks: [],
    })

    // 其余一律诚实空（`{items:[]}` 形状，避免无关 4xx / undefined 崩溃）。
    return json({ total: 0, items: [] })
  })
}

/**
 * 登录 + 预置「当前会话」：把 `forgeflow.conversation.<userId>` 指向 `SESSION_ID`，
 * 使首页 `useChatThread` 恢复该会话 ⇒ 三栏**开箱即进入同一上下文**（无需提交）。
 */
async function signIn(page: Page, withSession = true) {
  await page.addInitScript(
    ({ sessionId, withSessionFlag }) => {
      window.sessionStorage.setItem('forgeflow.jwt', 'e2e-token')
      window.sessionStorage.setItem('forgeflow.role', 'admin')
      window.sessionStorage.setItem('forgeflow.user', 'e2e-qa')
      if (withSessionFlag) {
        window.sessionStorage.setItem('forgeflow.conversation.e2e-qa', sessionId)
      }
    },
    { sessionId: SESSION_ID, withSessionFlag: withSession },
  )
}

test.describe('AgentFlow 2.0 / Phase B 三栏 Agent 工作台', () => {
  test('三栏同处一个上下文：任务→文件/数据→Agent→Run→Result，右栏随选择切换', async ({ page }) => {
    await signIn(page)
    await stub(page)
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()

    // ── 三栏骨架（Phase B 新 testid）───────────────────────────────────────────
    await expect(page.getByTestId('workbench')).toBeVisible()
    const contextCol = page.getByTestId('workbench-context')
    const centerCol = page.getByTestId('workbench-center')
    const inspectorCol = page.getByTestId('workbench-inspector')
    await expect(contextCol).toBeVisible()
    await expect(centerCol).toBeVisible()
    await expect(inspectorCol).not.toBeVisible()

    // 宽屏（1280px，> 1100px 断点）三栏**并排**：左 < 中 < 右（反空过阳性对照）。
    const ctxBox = await contextCol.boundingBox()
    const centerBox = await centerCol.boundingBox()
    expect(ctxBox && centerBox).toBeTruthy()
    expect(centerBox!.y).toBeGreaterThan(ctxBox!.y)
    expect(centerBox!.width).toBeGreaterThan(600)

    // ── 左栏：Task → 文件/数据 → Skill（「本次涉及」对象树）─────────────────────
    const runItem = page.getByTestId('workbench-context-run')
    await expect(runItem).toHaveCount(1)
    await expect(runItem).toContainText(INTENT) // Task
    const artifactsGroup = page.getByTestId('workbench-context-group-artifacts')
    await expect(artifactsGroup).toContainText(FILENAME) // 文件/数据
    // 前端契约（非后端能力声明）：后端**给了** injected ⇒ 左栏如实渲染出 Skill 名。
    // 后端能否产出 injected 由 `tests/unit/test_tb06_codeplane_injected_survives.py` 证明。
    await expect(page.getByTestId('workbench-context-group-skills')).toContainText('文档重写') // Skill

    // ── 中栏：Agent 工作流 · 对话 · 结果（既有 chat-workspace）─────────────────
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
    await expect(page.getByTestId('chat-agent-message')).toHaveCount(1)
    await expect(page.getByTestId('chat-agent-reply')).toHaveText(REPLY)
    const chip = page.getByTestId('chat-artifact-chip')
    await expect(chip).toHaveCount(1)
    await expect(chip).toContainText(FILENAME) // Result

    // ── 右栏初始空态 ──────────────────────────────────────────────────────────
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')

    // ── 左栏点击「运行」⇒ 右栏 Run 详情（三栏联动①）────────────────────────────
    await runItem.click()
    const panel = page.getByTestId('workbench-inspector-panel')
    await expect(panel).toContainText('运行状态')
    await expect(panel).toContainText('已完成')
    await expect(page.getByTestId('workbench-context-run')).toHaveAttribute('aria-current', 'true')

    await page.getByTestId('workbench-context-group-skills').getByRole('button', { name: '文档重写' }).click()
    await expect(panel).toContainText('按指令重写文档')
    await expect(panel).toContainText('2.1')

    // ── 中栏产物 chip「详情」⇒ 右栏切换到 Artifact 详情（T-B04 onInspect 接线）──
    await page.getByTestId('workbench-inspector-close').click()
    await page.getByTestId('chat-artifact-inspect').click()
    await expect(panel).toContainText(FILENAME)
    await expect(panel).toContainText('类型')

    // ── 中栏「检查运行」（点击消息）⇒ 右栏切回 Run 详情 ────────────────────────
    await page.getByTestId('workbench-inspector-close').click()
    await page.getByTestId('chat-agent-inspect-run').click()
    await expect(panel).toContainText('运行状态')

    // ── 关闭 ⇒ 右栏回到诚实空态 ───────────────────────────────────────────────
    await page.getByTestId('workbench-inspector-close').click()
    await expect(page.getByTestId('workbench-inspector-empty')).toHaveText('选择一个对象查看详情')
  })

  /**
   * 阴性对照（T-B06）——**不掩盖真相**的核心：当后端返回的 `codeplane` 里**没有**
   * `injected`（正是修复前 `_assemble_codeplane` 的真实形状）时，左栏「本次涉及 ·
   * Skill」组必须回落到**诚实空态**、「本次涉及 · 记忆」亦然 —— 前端**绝不臆造**
   * 一个从未注入的 Skill。若谁在左栏硬编码/伪造 Skill，本例即转红。
   */
  test('阴性对照：后端未给 codeplane.injected ⇒ 左栏 Skill 组为诚实空态', async ({ page }) => {
    await signIn(page)
    await stub(page, /* withInjected */ false)
    await page.goto('/')

    const skillsGroup = page.getByTestId('workbench-context-group-skills')
    await expect(skillsGroup).toContainText('本次未调用技能')
    await expect(skillsGroup).not.toContainText('文档重写')
    // 记忆组同样诚实空。
    await expect(page.getByTestId('workbench-context-group-memory')).toContainText('本次未使用记忆')

    // 中栏仍能正常工作（对照：并非整页崩溃，而是**只有** Skill/记忆如实为空）。
    await expect(page.getByTestId('chat-workspace')).toBeVisible()
    await expect(page.getByTestId('chat-agent-reply')).toHaveText(REPLY)
  })

  test('窄屏 390px：三栏折叠为单列且无横向溢出', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await signIn(page)
    await stub(page)
    await page.goto('/')
    await page.getByRole('button', { name: '任务上下文与文件' }).click()

    await expect(page.getByTestId('workbench')).toBeVisible()
    await expect(page.getByTestId('chat-workspace')).toBeVisible()

    // 折叠为单列：左 / 中 / 右 **同 x 且自上而下堆叠**（无反空过 —— 宽屏并排已在上个用例证明）。
    const contextCol = page.getByTestId('workbench-context')
    const centerCol = page.getByTestId('workbench-center')
    const inspectorCol = page.getByTestId('workbench-inspector')
    const ctxBox = await contextCol.boundingBox()
    const centerBox = await centerCol.boundingBox()
    expect(ctxBox && centerBox).toBeTruthy()
    expect(Math.abs(centerBox!.x - ctxBox!.x)).toBeLessThanOrEqual(2)
    expect(centerBox!.y).toBeGreaterThan(ctxBox!.y)
    await expect(inspectorCol).not.toBeVisible()

    // 无横向溢出：文档 scrollWidth 不超出视口宽度（容 1px 子像素）。
    const metrics = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      docClient: document.documentElement.clientWidth,
      innerWidth: window.innerWidth,
    }))
    expect(metrics.docScroll).toBeLessThanOrEqual(metrics.docClient + 1)
    expect(metrics.docScroll).toBeLessThanOrEqual(metrics.innerWidth + 1)
  })
})
