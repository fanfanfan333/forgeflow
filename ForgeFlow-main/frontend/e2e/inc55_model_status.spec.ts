import { emptyApiResponse } from './apiFallback'
/**
 * INC55 缺陷② —— 模型标识「模型：未记录」的真实浏览器交叉确认。
 *
 * 两段根因（本 spec 各自独立钉住）：
 *   ① `deriveModelInfo()` 把**空对象** `{}` 误判成「有记录但 models 为空」⇒ 落到
 *      「模型：未记录」分支。修复后 `null` / `undefined` / `{}` **三者**都走「无记录」。
 *   ② 首页 composer 只绑定**最新一条 run**；该 run 的 `llm` 未记录时无从展示。
 *      修复后：有记录 ⇒ 显示该 run 真实模型；无记录 ⇒ 显示**平台配置**模型（带
 *      「平台配置」标签，绝不写「已连接」）；平台也未知 ⇒ 诚实空态。
 *
 * 判定纪律：在 `/api/**` 边界打桩，专注前端在**给定后端响应**下的真实渲染；断言用会
 * 重试的 `toContainText` / `toHaveCount`。单元断言直接 import 源模块（纯函数，无运行时依赖）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

import { deriveModelInfo } from '../src/views/runs/modelInfo'
import {
  RUN_DETAIL_POLL_MS,
  TERMINAL_RUN_STATUSES,
  runDetailRefetchInterval,
} from '../src/api/runPolling'

/* ------------------------------------------------------------------------- *
 * A. 纯函数单元断言（Node 侧，无需浏览器）—— null / undefined / {} 一律 null
 * ------------------------------------------------------------------------- */
test.describe('INC55 缺陷② —— deriveModelInfo 空态口径', () => {
  test('null / undefined / {} 三者均 ⇒ null（空对象不再误判成「有记录」）', () => {
    expect(deriveModelInfo(null)).toBeNull()
    expect(deriveModelInfo(undefined)).toBeNull()
    // 空对象：后端对未保留明细的 run 返回 `llm = {}`（无任何键）。
    expect(deriveModelInfo({} as never)).toBeNull()
  })

  test('阳性对照：真实记录 / 「有记录但 models 空」语义不被误伤', () => {
    // 有真实模型 ⇒ 非 null，且可用。
    const real = deriveModelInfo({
      models: [
        { slot: 'strong', llm_type: 'chat-ollama', model: 'qwen3:8b', base_url: 'http://localhost:11434' },
      ],
    } as never)
    expect(real).not.toBeNull()
    expect(real?.available).toBe(true)
    expect(real?.model).toBe('qwen3:8b')

    // 有记录但 models 为空（非空对象）⇒ 「模型：未记录」，与 `{}` 明确区分。
    const recordedEmpty = deriveModelInfo({ runtime_mode: 'react', models: [] } as never)
    expect(recordedEmpty).not.toBeNull()
    expect(recordedEmpty?.available).toBe(false)
    expect(recordedEmpty?.reason).toBe('模型：未记录')
  })
})

/* ------------------------------------------------------------------------- *
 * B. 浏览器渲染 —— 首页 composer 的三态
 * ------------------------------------------------------------------------- */
const RUN_ID = 'run-inc55'

type Case = {
  /** 最新 run 的 `GET /runs/{id}`.llm（`{}` = 无记录）。 */
  runLlm: unknown
  /** `GET /runs/model-status` 的正文。 */
  platform: unknown
}

const cfg: Case = { runLlm: {}, platform: { provider: 'ollama', models: [], probed: false } }

function reset(over: Partial<Case>) {
  Object.assign(cfg, { runLlm: {}, platform: { provider: 'ollama', models: [], probed: false } }, over)
}

async function signIn(page: Page) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('forgeflow.jwt', 'e2e-inc55-token')
    window.sessionStorage.setItem('forgeflow.role', 'admin')
    window.sessionStorage.setItem('forgeflow.user', 'e2e-inc55')
  })
}

async function stub(page: Page) {
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 平台配置模型（INC55 新增端点）。
    if (path === '/api/runs/model-status' && method === 'GET') return json(cfg.platform)

    // 最新一条 run（首页 composer 的模型标识绑定它）。
    if (path === '/api/runs' && method === 'GET') {
      return json({
        total: 1,
        items: [
          {
            run_id: RUN_ID,
            thread_id: 'th-inc55',
            status: 'completed',
            outcome: 'success',
            intent: '检查文件',
            title: '检查文件',
            created_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:01:00Z',
            experience_id: null,
            step_count: 0,
            session_id: RUN_ID,
            parent_run_id: null,
          },
        ],
      })
    }

    const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
    if (detailM && method === 'GET') {
      return json({
        run_id: detailM[1],
        thread_id: 'th-inc55',
        status: 'completed',
        outcome: 'success',
        intent: '检查文件',
        steps: [],
        errors: [],
        created_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:01:00Z',
        experience_id: null,
        runtime_mode: 'react',
        // INC55：未保留执行明细的 run ⇒ llm 为空对象 `{}`。
        llm: cfg.runLlm,
        detail_retained: false,
        tool_invocations: [],
        artifacts: [],
        session_id: RUN_ID,
        parent_run_id: null,
      })
    }

    // 外壳噪音：一律诚实空。
    if (path === '/api/metrics/' || path === '/api/metrics') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/workspace/sessions') return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
    if (path === '/api/health') return json({ status: 'ok' })
    if (/^\/api\/agents/.test(path)) return json([])
    if (path === '/api/skills' && method === 'GET') return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
    if (path === '/api/skill-candidates') return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
    if (path === '/api/experiences') return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
    if (path === '/api/resources/limits')
      return json({ max_bytes: 1048576, supported_extensions: ['.txt'], note: '' })
    if (path === '/api/resources' && method === 'GET') return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
  })
}

async function bootHome(page: Page, over: Partial<Case>) {
  reset(over)
  await signIn(page)
  await stub(page)
  await page.goto('/')
  await expect(page.getByTestId('chat-composer')).toBeVisible()
}

test.describe('INC55 缺陷② —— 首页模型标识三态', () => {
  test('无 run 记录 + 平台配置可用 ⇒ 显示平台配置模型并标注「平台配置」', async ({ page }) => {
    await bootHome(page, {
      runLlm: {}, // 空对象：后端未记录
      platform: {
        provider: 'ollama',
        models: [
          { slot: 'strong', llm_type: 'chat-ollama', model: 'qwen3:8b', base_url: 'http://localhost:11434' },
        ],
        probed: false,
      },
    })
    const ms = page.getByTestId('model-status')
    await expect(ms).toContainText('qwen3:8b')
    await expect(ms).toContainText('平台配置')
    // 诚实纪律：绝不写成「已连接」，绝不退回「模型：未记录」。
    await expect(ms).not.toContainText('已连接')
    await expect(ms).not.toContainText('模型：未记录')
    await expect(ms).toHaveAttribute('data-source', 'platform')
  })

  test('无 run 记录 + 平台也未知 ⇒ 诚实空态（不编造模型）', async ({ page }) => {
    await bootHome(page, {
      runLlm: {},
      platform: { provider: '', models: [], probed: false },
    })
    const ms = page.getByTestId('model-status')
    await expect(ms).toContainText('暂无运行记录')
    await expect(ms).not.toContainText('平台配置')
  })

  test('有 run 记录 ⇒ 优先显示该 run 实际使用的模型（不带「平台配置」）', async ({ page }) => {
    await bootHome(page, {
      runLlm: {
        runtime_mode: 'react',
        models: [
          { slot: 'strong', llm_type: 'chat-ollama', model: 'qwen3:8b', base_url: 'http://localhost:11434' },
        ],
      },
      platform: {
        provider: 'ollama',
        models: [{ slot: 'strong', llm_type: 'chat-ollama', model: 'SHOULD-NOT-RENDER', base_url: '' }],
        probed: false,
      },
    })
    const ms = page.getByTestId('model-status')
    await expect(ms).toContainText('qwen3:8b')
    await expect(ms).not.toContainText('平台配置')
    await expect(ms).not.toContainText('SHOULD-NOT-RENDER')
  })
})

/* ------------------------------------------------------------------------- *
 * C. runPolling 纯函数规则（Node 侧）—— 与后端 TERMINAL_STATUSES 同口径
 * ------------------------------------------------------------------------- */
test.describe('INC55 缺陷② —— runDetailRefetchInterval 轮询口径', () => {
  test('null / undefined ⇒ false（详情未取到 / 无 runId，不空转）', () => {
    expect(runDetailRefetchInterval(undefined)).toBe(false)
    expect(runDetailRefetchInterval(null)).toBe(false)
  })

  test('非终态 running / queued ⇒ RUN_DETAIL_POLL_MS（3_000）', () => {
    expect(RUN_DETAIL_POLL_MS).toBe(3_000)
    expect(runDetailRefetchInterval('running')).toBe(RUN_DETAIL_POLL_MS)
    expect(runDetailRefetchInterval('queued')).toBe(RUN_DETAIL_POLL_MS)
  })

  test('五个终态值各自 ⇒ false（终态即停）', () => {
    for (const s of TERMINAL_RUN_STATUSES) {
      expect(runDetailRefetchInterval(s)).toBe(false)
    }
  })
})

/* ------------------------------------------------------------------------- *
 * D. 浏览器反事实 —— 缓存新鲜度：running(空 llm) ⇒ completed(真实 llm) 自动升级
 *
 *    反事实纪律：把 `useRunDetail` 的 `refetchInterval` 去掉后，本用例**必须失败**
 *    （页面会永远停在「平台配置」）。这是「轮询真的在起作用」的可证伪证据。
 * ------------------------------------------------------------------------- */
const FRESH_RUN_ID = 'run-inc55-fresh'

test.describe('INC55 缺陷② —— 运行详情缓存新鲜度（反事实）', () => {
  test('running（空 llm）⇒ completed（真实 llm）：自动从「平台配置」升级为本次运行模型', async ({
    page,
  }) => {
    await signIn(page)

    // 时间驱动的状态：详情请求开始后的头 2s 一律 `running` + `llm:{}`，之后一律
    // `completed` + 真实 `llm`。这样只有「真的按周期重取」才能观察到升级——对
    // StrictMode 双挂载 / 重复请求也稳健（它们都落在前 2s 窗口内，仍是 running）。
    const RUNNING_WINDOW_MS = 2_000
    let firstDetailAt: number | null = null

    await page.route('**/api/**', async (route: Route) => {
      const url = new URL(route.request().url())
      const path = url.pathname
      const method = route.request().method()
      const reply = (body: unknown, status = 200) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

      // 平台**配置**模型：故意用可辨识名字，以区分「回退」与「真实升级」。
      if (path === '/api/runs/model-status' && method === 'GET') {
        return reply({
          provider: 'ollama',
          models: [
            { slot: 'strong', llm_type: 'chat-ollama', model: 'PLATFORM-CONFIGURED', base_url: '' },
          ],
          probed: false,
        })
      }

      if (path === '/api/runs' && method === 'GET') {
        return reply({
          total: 1,
          items: [
            {
              run_id: FRESH_RUN_ID,
              thread_id: 'th-inc55',
              status: 'running',
              outcome: 'running',
              intent: '检查文件',
              title: '检查文件',
              created_at: '2026-01-01T00:00:00Z',
              completed_at: null,
              experience_id: null,
              step_count: 0,
              session_id: FRESH_RUN_ID,
              parent_run_id: null,
            },
          ],
        })
      }

      const detailM = /^\/api\/runs\/([^/]+)$/.exec(path)
      if (detailM && method === 'GET') {
        if (firstDetailAt === null) firstDetailAt = Date.now()
        const done = Date.now() - firstDetailAt >= RUNNING_WINDOW_MS
        return reply({
          run_id: detailM[1],
          thread_id: 'th-inc55',
          status: done ? 'completed' : 'running',
          outcome: done ? 'success' : 'running',
          intent: '检查文件',
          steps: [],
          errors: [],
          created_at: '2026-01-01T00:00:00Z',
          completed_at: done ? '2026-01-01T00:01:00Z' : null,
          experience_id: null,
          runtime_mode: 'react',
          // 与真实后端一致：running 期 `llm = {}`；completed 期才有真实模型。
          llm: done
            ? {
                runtime_mode: 'react',
                models: [
                  {
                    slot: 'strong',
                    llm_type: 'chat-ollama',
                    model: 'qwen3:8b',
                    base_url: 'http://localhost:11434',
                  },
                ],
              }
            : {},
          detail_retained: done,
          tool_invocations: [],
          artifacts: [],
          session_id: FRESH_RUN_ID,
          parent_run_id: null,
        })
      }

      // 外壳噪音：一律诚实空。
      if (path === '/api/metrics/' || path === '/api/metrics') return reply({ has_data: false })
      if (path === '/api/cost/savings') return reply({ has_data: false })
      if (path === '/api/workspace/sessions') return reply({ total: 0, items: [] })
      if (path === '/api/health') return reply({ status: 'ok' })
      if (/^\/api\/agents/.test(path)) return reply([])
      if (path === '/api/skills' && method === 'GET') return reply({ total: 0, items: [] })
      if (path === '/api/skill-candidates') return reply({ total: 0, items: [] })
      if (path === '/api/experiences') return reply({ total: 0, items: [] })
      if (path === '/api/resources/limits')
        return reply({ max_bytes: 1048576, supported_extensions: ['.txt'], note: '' })
      if (path === '/api/resources' && method === 'GET') return reply({ total: 0, items: [] })
      return reply({ total: 0, items: [] })
    })

    await page.goto('/')
    await expect(page.getByTestId('chat-composer')).toBeVisible()

    const ms = page.getByTestId('model-status')
    // 阶段一：详情为 running + `llm:{}` ⇒ 无本次运行记录 ⇒ 回退显示**平台配置**模型。
    await expect(ms).toContainText('平台配置')
    await expect(ms).toContainText('PLATFORM-CONFIGURED')
    await expect(ms).toHaveAttribute('data-source', 'platform')

    // 阶段二：让出 ≥1 个轮询周期（RUN_DETAIL_POLL_MS=3s > RUNNING_WINDOW_MS=2s）
    // ⇒ 详情重取变 completed + 真实 llm ⇒ 自动升级为本次运行真实模型。
    await expect(ms).toContainText('qwen3:8b', { timeout: 10_000 })
    await expect(ms).not.toContainText('平台配置')
    await expect(ms).not.toContainText('PLATFORM-CONFIGURED')
    await expect(ms).not.toHaveAttribute('data-source', 'platform')
  })
})
