/**
 * INC41 —— 首页内联流式会话（INC-INLINE-STREAMING）**真实浏览器** e2e 钉子。
 *
 * 目的（team-lead FD-1 ~ FD-4 的四条修复，各带**真值翻转**判据）：
 *   · FD-1 负向：`run.token(半截)` → `run.turn{interrupted:true}` → `run.final_answer{完整}` →
 *     `run.completed` ⇒ `conv-inline-aborted` **count 0**（不再「完整答案 + 已中断」自相矛盾）。
 *   · FD-1 正向对照：同序列去掉 final_answer / completed、只留 `run.aborted` ⇒ **count 1** 且逐字
 *     「已中断，以下内容不完整」。（两条都要，否则判据永真不红。）
 *   · FD-2：SSE 失败（500）走 2s 轮询兜底，`detail.status='aborted'` ⇒ 合成 `run.aborted`
 *     （改前被谎报 `run.completed`）⇒ 中断横幅出现。
 *   · FD-3：`FollowUpComposer` 的 `data-testid="conv-followup"` **字面量保留**；内联面板新 testid
 *     由**外层容器**承载（`conv-inline-followup`）；`/tasks` 默认档 `#conv-followup-input` 仍在。
 *   · FD-4：`run.turn{channel:'narration'}` 的临时文本**不再**「打出来又消失」；answer 轮出现后取代。
 *
 * 网络边界（沿用 inc32/inc36/inc37 口径）：在 `/api/**` 边界 route 打桩，专注前端在给定后端
 * 响应下的真实交互；后端语义不在本 spec 范围。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

const RUN_ID = 'run_inc41_1'
const INTENT = 'INC41 内联流式 e2e：验证打字机与中断口径'

type Cfg = {
  /** SSE 原始帧字符串；`null` ⇒ `/events` 返回 500（走轮询兜底）。 */
  sseBody: string | null
  /** `GET /runs/{id}` 的 status（决定终态口径）。 */
  status: string
}
const cfg: Cfg = { sseBody: null, status: 'completed' }

function frame(type: string, data: Record<string, unknown>, seq: number) {
  return { run_id: RUN_ID, type, data, seq, ts: '2026-01-01T00:00:00Z' }
}

/** 把一组帧包成 SSE 文本（末帧恒 `[DONE]`，与后端收尾一致）。 */
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

    // 运行事件流：成功 ⇒ 给定帧；失败 ⇒ 500（触发 `useRunEvents` 的轮询兜底）。
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
        thread_id: 't',
        status: cfg.status,
        outcome: cfg.status === 'completed' ? 'success' : '',
        intent: INTENT,
        steps: [],
        errors: [],
        created_at: '2026-01-01T00:00:00Z',
        completed_at: cfg.status === 'completed' ? '2026-01-01T00:01:00Z' : null,
        experience_id: null,
        session_id: '',
        parent_run_id: null,
        runtime_mode: 'llm',
      })
    }

    // 运行列表（首页「近期任务」+ /tasks 左列的数据源）。
    if (path === '/api/runs' && method === 'GET') {
      return json({
        total: 1,
        items: [
          {
            run_id: RUN_ID,
            thread_id: 't',
            status: cfg.status,
            outcome: '',
            intent: INTENT,
            title: INTENT,
            created_at: '2026-01-01T00:00:00Z',
            completed_at: null,
            experience_id: null,
            step_count: 0,
            session_id: '',
            parent_run_id: null,
          },
        ],
      })
    }

    if (path === '/api/workspace/sessions' && method === 'GET') return json({ total: 0, items: [] })
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/health') return json({ status: 'ok' })
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })

    // 其余一律诚实空（`{items:[]}` 形状，避免无关 4xx / undefined 崩溃）。
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

/** 打开首页并点「近期任务」的一行，就地展开内联会话面板。 */
async function bootHome(page: Page, c: Partial<Cfg>) {
  cfg.sseBody = c.sseBody ?? null
  cfg.status = c.status ?? 'completed'
  await signIn(page)
  await stub(page)
  await page.goto('/')
  const row = page.getByTestId('conv-inline-history-row').first()
  await expect(row).toBeVisible()
  await row.click()
  await expect(page.getByTestId('conv-inline-panel')).toBeVisible()
  await expect(page.getByTestId('conv-inline-panel')).toHaveAttribute('data-run-id', RUN_ID)
}

test.describe('INC41 内联流式会话', () => {
  // FD-1 负向：完整答案 + completed ⇒ 无中断横幅。
  test('FD-1 负向：半截后收到完整 final_answer + completed ⇒ conv-inline-aborted count 0', async ({
    page,
  }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([
        frame('run.token', { turn: 0, channel: 'pending', fragment: '最终答案' }, 1),
        frame('run.turn', { turn: 0, channel: 'pending', text: '最终答案', interrupted: true }, 2),
        frame('run.final_answer', { text: '最终答案', iteration: 1 }, 3),
        frame('run.completed', { status: 'completed' }, 4),
      ]),
    })
    await expect(page.getByTestId('conv-inline-answer')).toHaveText('最终答案')
    await expect(page.getByTestId('conv-inline-aborted')).toHaveCount(0)
  })

  // FD-1 正向对照：只有 run.aborted ⇒ 横幅 count 1 且逐字。
  test('FD-1 正向对照：同序列仅留 run.aborted ⇒ 横幅 count 1 且逐字「已中断，以下内容不完整」', async ({
    page,
  }) => {
    await bootHome(page, {
      status: 'aborted',
      sseBody: sse([
        frame('run.token', { turn: 0, channel: 'pending', fragment: '半截' }, 1),
        frame('run.turn', { turn: 0, channel: 'pending', text: '半截', interrupted: true }, 2),
        frame('run.aborted', { status: 'aborted' }, 3),
      ]),
    })
    await expect(page.getByTestId('conv-inline-aborted')).toHaveText('已中断，以下内容不完整')
    await expect(page.getByTestId('conv-inline-answer')).toHaveText('半截')
  })

  // FD-2：轮询兜底把 aborted 诚实合成为 run.aborted（不谎报 completed）。
  test('FD-2 轮询兜底：SSE 失败 + status=aborted ⇒ 合成 run.aborted ⇒ 横幅出现', async ({ page }) => {
    await bootHome(page, { sseBody: null, status: 'aborted' })
    await expect(page.getByTestId('workspace-live-degraded')).toBeVisible()
    await expect(page.getByTestId('conv-inline-aborted')).toHaveText('已中断，以下内容不完整')
  })

  // FD-4：narration 归类的临时文本保留（不打出来又消失）。
  test('FD-4：narration 轮临时文本保留 ⇒ conv-inline-answer 文本 = 旁白甲', async ({ page }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([
        frame('run.token', { turn: 0, channel: 'pending', fragment: '旁白甲' }, 1),
        frame('run.turn', { turn: 0, channel: 'narration', text: '旁白甲', interrupted: false }, 2),
        frame('run.completed', { status: 'completed' }, 3),
      ]),
    })
    await expect(page.getByTestId('conv-inline-answer')).toHaveText('旁白甲')
  })

  // FD-4 阳性对照：answer 轮出现后取代临时旁白。
  test('FD-4 阳性对照：answer 轮出现 ⇒ conv-inline-answer 文本 = 答案', async ({ page }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([
        frame('run.token', { turn: 0, channel: 'pending', fragment: '旁白甲' }, 1),
        frame('run.turn', { turn: 0, channel: 'narration', text: '旁白甲', interrupted: false }, 2),
        frame('run.token', { turn: 1, channel: 'pending', fragment: '答案' }, 3),
        frame('run.turn', { turn: 1, channel: 'answer', text: '答案', interrupted: false }, 4),
        frame('run.completed', { status: 'completed' }, 5),
      ]),
    })
    await expect(page.getByTestId('conv-inline-answer')).toHaveText('答案')
  })

  // FD-3：字面量 testid 保留 + 新 testid 由外层容器承载。
  test('FD-3：conv-followup 字面量保留 + conv-inline-followup 承载新 testid + 输入框可达', async ({
    page,
  }) => {
    await bootHome(page, {
      status: 'completed',
      sseBody: sse([frame('run.completed', { status: 'completed' }, 1)]),
    })
    // 内联面板：新 testid 由外层容器承载，输入框 `#conv-inline-followup-input` 可达。
    await expect(page.getByTestId('conv-inline-followup')).toHaveCount(1)
    await expect(page.locator('#conv-inline-followup-input')).toHaveCount(1)
    // 内层表单仍是**字面量** testid `conv-followup`，且发送按钮可点。
    await expect(page.getByTestId('conv-followup')).toHaveCount(1)
    await expect(page.getByTestId('conv-inline-followup').getByRole('button')).toHaveCount(1)
  })

  // FD-3 默认档钉子：/tasks 的 FollowUpComposer 用默认 testid / inputId。
  test('FD-3 默认档：/tasks 表单 testid=conv-followup、输入框 id=conv-followup-input', async ({
    page,
  }) => {
    cfg.sseBody = sse([frame('run.completed', { status: 'completed' }, 1)])
    cfg.status = 'completed'
    await signIn(page)
    await stub(page)
    await page.goto('/tasks')
    await expect(page.getByTestId('conv-followup')).toHaveCount(1)
    await expect(page.locator('#conv-followup-input')).toHaveCount(1)
  })
})
