/**
 * INC39 —— 结果层「Agent 对话式完成态」**真实浏览器** e2e（`frontend/e2e`）。
 *
 * 背景（本仓教训）：INC24 的段⑤ 曾固定渲染**三个按钮**（「让智能体处理」/「我自己处理」/
 * 「查看变更」）+ 一个重复的续聊输入框，让页面像**审批 / 工作流系统**；无真实结论时又把
 * 「本次运行未启用模型驱动」伪装成 **Agent 的一句话结论**。INC39 的重构若零覆盖，「旧套件
 * 全绿」就又会把「新行为已上线」当成事实。故本 spec 专为 INC39 **新增**用例，机械判定：
 *
 *   ① 三固定按钮 + 段内输入框**已消失**；续聊输入框**唯一**（中列底部 `conv-followup`）。
 *   ② 上下文快捷操作 **0～3 条**且**由任务类型决定**（三组 testid 集合互不相同）。
 *   ③ Diff 红线**双向对照**：无真实变更 ⇒ 无任何 Diff 动作；有变更 ⇒ 该动作出现且可见。
 *   ④ 执行环境状态**不伪装**：非模型驱动 ⇒ `result-env-status` 可见且「未启用模型」**不**
 *      进 `#res-panel-result`（尤其**不是** Agent 的一句话结论）；模型驱动 ⇒ 该条不存在。
 *   ⑤ 快捷操作**点击有真实可见效果**（continue 真发请求 / export 真下载 / sources 真切 Tab）。
 *   ⑥ 不空转：无特征产物 ⇒ `result-contextual-actions` **整段不进 DOM**（无空壳、无话术）。
 *   ⑦ 底部 follow-up placeholder **逐字**且提交真发 `POST /workspace/tasks`（带 `parent_run_id`
 *      **与** `continued_from_artifact_ref`）。
 *
 * ── 反证纪律（本 spec 的硬要求）──────────────────────────────────────────────
 * 「断言全绿」本身不是证据 —— `toHaveCount(0)` 若选择器写错会**永远绿**。故：
 *   · 每条「不存在」断言都配**阳性对照**（临时注入同 testid 元素，证明选择器真能命中，见
 *     `expectAbsentWithControl`）；
 *   · 「≤3」「互不相同」「逐字 placeholder」配**判别力对照**（注入第 4 个按钮 / 断言排除
 *     近似串），证明阈值/逐字比较真的会红。
 *
 * ── 网络边界（分层诚实，沿用 inc32/inc36 spec 的既有口径）────────────────────
 * 在 `/api/**` 边界做 route 拦截（stub 后端），专注验证**前端**在给定后端响应下的真实交互。
 * 后端语义不在本 spec 范围。骨架与 payload 构造函数**照抄** `inc36_conversation.spec.ts`。
 *
 * 引文纪律：一律 `文件名::符号名`。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

declare global {
  interface Window {
    /** 记录「程序化下载锚点」的钩子（见 boot 的 addInitScript；复用 inc32 写法）。 */
    __ffDownloads?: { href: string | null; download: string | null }[]
  }
}

const RUN_ID = 'run_inc39_1'
const INTENT = 'INC39 结果层对话式完成态：整理客户反馈'
const FOLLOWUP_TEXT = '继续：把结论整理成一页摘要'

/** 底部统一续聊输入框的 placeholder（**逐字**抄自 `FollowUpComposer.tsx`）。 */
const FOLLOWUP_PLACEHOLDER = '继续告诉 AI 你想怎么处理……'
/** 知识档 `continue` 提交的业务指令（**逐字**抄自 `resultActions.ts::INSTRUCTION_FOLLOW_UP`）。 */
const INSTRUCTION_FOLLOW_UP = '基于本次检索到的资料继续追问，补充更需要确认的细节。'
/** 主产物指纹（`continued_from_artifact_ref` 的逐字对照值）。 */
const PRIMARY_REF = 'inc39-primary-ref'

/** 真实 unified diff（供 `realRun.deriveCodeDiff::parseUnifiedDiff` 解析出 1 文件 1+/1-）。 */
const CODE_DIFF = [
  'diff --git a/foo.py b/foo.py',
  'index 1111111..2222222 100644',
  '--- a/foo.py',
  '+++ b/foo.py',
  '@@ -1,2 +1,2 @@',
  '-broken()',
  '+fixed()',
  '',
].join('\n')

/** 富产物（含「最终答案」散文行 + 列表项）⇒ 结论 + 关键发现都非空。 */
const RICH_ARTIFACT_CONTENT = [
  '# 运行报告',
  '',
  '**意图**：整理客户反馈',
  '',
  '## 最终答案',
  '',
  '本次反馈整理已完成，共归纳出三类主要问题。',
  '- 问题一类：登录超时',
  '- 问题二类：导出失败',
  '',
].join('\n')

/** 数据档产物（「最终答案」下**只有列表项** ⇒ findings>0，metrics=0）。 */
const DATA_ARTIFACT_CONTENT = [
  '# 运行报告',
  '',
  '**意图**：分析销售指标',
  '',
  '## 最终答案',
  '',
  '- 转化率环比上升 12%',
  '- 客单价环比下降 3%',
  '',
].join('\n')

/** 无 `##` 小节 ⇒ 无结论 / 无发现 / 无指标（知识档、兜底档、环境档共用）。 */
const PLAIN_ARTIFACT_CONTENT = ['# 运行报告', '', '**意图**：检索资料', ''].join('\n')

/** 模型驱动档产物：一句真实散文结论 ⇒ `result-headline` 有真值。 */
const MODEL_ARTIFACT_CONTENT = [
  '# 运行报告',
  '',
  '**意图**：分析销售线索',
  '',
  '## 最终答案',
  '',
  '本次分析已完成，线索质量整体良好。',
  '',
].join('\n')

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

function art(over: Partial<Artifact> & { id: string }): Artifact {
  return {
    id: over.id,
    kind: over.kind ?? 'report_markdown',
    title: over.title ?? '运行报告.md',
    format: over.format ?? 'markdown',
    content: over.content ?? '',
    source: over.source ?? 'report.render',
    result_ref: over.result_ref ?? `ref_${over.id}`,
    created_at: over.created_at ?? '2026-01-01T00:00:10Z',
  }
}

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
  step_count: number
  session_id: string
  parent_run_id: string | null
  steps: { index: number; status: string; tool?: string; note?: string }[]
  errors: string[]
  artifacts: Artifact[]
  plan?: { steps: { index: number; tool: string; note: string }[] }
  tool_invocations?: {
    tool: string
    status: string
    executed: boolean
    step_id: string
    latency_ms: number
    payload: unknown
  }[]
  runtime_mode?: string
  llm?: Record<string, unknown> | null
  codeplane?: Record<string, unknown>
}

function detail(over: Partial<RunLike> & { run_id: string }): RunLike {
  return {
    run_id: over.run_id,
    thread_id: over.thread_id ?? `thread_${over.run_id}`,
    status: over.status ?? 'completed',
    outcome: over.outcome ?? 'success',
    intent: over.intent ?? '任务',
    title: over.title ?? over.intent ?? '任务',
    created_at: over.created_at ?? '2026-01-01T00:00:00Z',
    completed_at: over.completed_at === undefined ? '2026-01-01T00:01:00Z' : over.completed_at,
    experience_id: over.experience_id ?? null,
    step_count: over.step_count ?? (over.steps?.length ?? 0),
    session_id: over.session_id ?? 'sess_inc39',
    parent_run_id: over.parent_run_id ?? null,
    steps: over.steps ?? [],
    errors: over.errors ?? [],
    artifacts: over.artifacts ?? [],
    plan: over.plan,
    tool_invocations: over.tool_invocations,
    runtime_mode: over.runtime_mode ?? 'deterministic',
    llm: over.llm ?? null,
    codeplane: over.codeplane,
  }
}

/** `RunDetail` → `RunSummary`（列表项形状）。 */
function summaryOf(d: RunLike) {
  const {
    steps,
    errors,
    artifacts,
    plan,
    tool_invocations,
    runtime_mode,
    llm,
    codeplane,
    ...rest
  } = d
  void errors
  void artifacts
  void plan
  void tool_invocations
  void runtime_mode
  void llm
  void codeplane
  return { ...rest, step_count: steps.length }
}

type TaskBody = { intent?: string; parent_run_id?: string; context?: Record<string, unknown> }
const rec: { workspaceTasks: { method: string; path: string; body: TaskBody }[] } = {
  workspaceTasks: [],
}

async function stubApi(page: Page, seeds: RunLike[]) {
  const db: { order: string[]; detail: Record<string, RunLike> } = {
    order: seeds.map((r) => r.run_id),
    detail: Object.fromEntries(seeds.map((r) => [r.run_id, r])),
  }

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // 派发（follow-up / continue）：捕获 method/path/body，并让新句柄可被选中。
    if (path === '/api/workspace/tasks' && method === 'POST') {
      const body = req.postDataJSON() as TaskBody
      rec.workspaceTasks.push({ method, path, body })
      const id = `run_new_${db.order.length + 1}`
      const created = detail({
        run_id: id,
        status: 'running',
        outcome: '',
        intent: body.intent ?? '',
        completed_at: null,
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

    // 运行事件流（SSE）：空流，让实时条立即收尾（本 spec 不验证实时流）。
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

async function boot(page: Page, opts: { runs?: RunLike[]; role?: string } = {}) {
  rec.workspaceTasks = []
  // 同一 test 内多次 boot（多组 payload）时先卸掉上一份 stub，避免路由叠加。
  try {
    await page.unroute('**/api/**')
  } catch {
    /* 首次 boot 时没有已注册的路由，忽略 */
  }
  await page.addInitScript((role: string) => {
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
  }, opts.role ?? 'admin')
  await stubApi(page, opts.runs ?? [richRun()])
  await page.goto('/tasks')
}

/** 目标 tabpanel 是否**可见**（`hidden` ⇒ 不可见）。 */
const panelVisible = (page: Page, id: string) =>
  page.evaluate((elId) => {
    const el = document.getElementById(elId)
    if (!el) return false
    return el.getBoundingClientRect().height > 0 && getComputedStyle(el).display !== 'none'
  }, id)

/**
 * 「不存在」断言的**阳性对照**封装 —— 先断言 `count=0`，再临时注入一个同 testid 元素并
 * 断言 `count=1`（证明选择器真能命中该 testid，否则 `count=0` 可能是选择器写错的**假绿**），
 * 最后移除并复原 `count=0`。
 */
async function expectAbsentWithControl(page: Page, testid: string, parentSelector = '#res-panel-result') {
  await expect(page.getByTestId(testid)).toHaveCount(0)
  await page.evaluate(
    ({ id, parent }) => {
      const host = document.querySelector(parent) ?? document.body
      const el = document.createElement('button')
      el.setAttribute('data-testid', id)
      el.setAttribute('data-ff-probe', 'inc39')
      host.appendChild(el)
    },
    { id: testid, parent: parentSelector },
  )
  await expect(page.getByTestId(testid)).toHaveCount(1)
  await page.evaluate(() => {
    for (const el of Array.from(document.querySelectorAll('[data-ff-probe="inc39"]'))) el.remove()
  })
  await expect(page.getByTestId(testid)).toHaveCount(0)
}

/** 注入一个临时元素（阳性对照），返回移除函数。 */
async function injectProbe(page: Page, selector: string, testid: string) {
  await page.evaluate(
    ({ sel, id }) => {
      const host = document.querySelector(sel) ?? document.body
      const el = document.createElement('button')
      el.setAttribute('data-testid', id)
      el.setAttribute('data-ff-probe', 'inc39')
      host.appendChild(el)
    },
    { sel: selector, id: testid },
  )
  return async () => {
    await page.evaluate(() => {
      for (const el of Array.from(document.querySelectorAll('[data-ff-probe="inc39"]'))) el.remove()
    })
  }
}

// --- payload 构造函数 -------------------------------------------------------- //

/** 富运行（结论 + 关键发现 + 真实来源）⇒ 面板信息最全，用于「三固定按钮已消失」。 */
function richRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'research.search' }],
    artifacts: [
      art({ id: 'art_rich', content: RICH_ARTIFACT_CONTENT, result_ref: PRIMARY_REF }),
    ],
    tool_invocations: [
      {
        tool: 'research.search',
        status: 'ok',
        executed: true,
        step_id: `${RUN_ID}:1:0`,
        latency_ms: 12,
        payload: { url: 'https://example.com/inc39-rich-source' },
      },
    ],
  })
}

/** 数据档：`findings > 0`（无代码面 / 无来源）。 */
function dataRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'data.query' }],
    artifacts: [art({ id: 'art_data', content: DATA_ARTIFACT_CONTENT })],
  })
}

/** 知识档：`sources > 0`（无代码面 / 无指标 / 无发现）。 */
function kbRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'research.search' }],
    artifacts: [art({ id: 'art_kb', content: PLAIN_ARTIFACT_CONTENT })],
    tool_invocations: [
      {
        tool: 'research.search',
        status: 'ok',
        executed: true,
        step_id: `${RUN_ID}:1:0`,
        latency_ms: 12,
        payload: { url: 'https://example.com/inc39-source' },
      },
    ],
  })
}

/** 兜底档：有产物但无任何特征内容（无代码面 / 无指标 / 无发现 / 无来源）。 */
function fallbackRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'report.render' }],
    artifacts: [art({ id: 'art_fb', content: PLAIN_ARTIFACT_CONTENT })],
  })
}

/** 不空转档：无产物、无代码面、无来源、无指标 / 发现。 */
function emptyRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'research.search' }],
    artifacts: [],
  })
}

/** 代码档：`codeplane.present`；`withDiff` 决定 `codeplane.diff` 是否为非空文本。 */
function codeRun(withDiff: boolean): RunLike {
  const codeplane: Record<string, unknown> = {
    engine: { available: true },
    workspace: { workspace_id: 'ws_inc39', state: 'active' },
    timeline: [
      {
        seq: 0,
        ts: '2026-01-01T00:00:01Z',
        phase: 'execute',
        kind: 'exec',
        status: 'ok',
        tool: 'code.run',
        label: '执行代码',
        detail: '',
        latency_ms: 12,
      },
    ],
  }
  if (withDiff) codeplane.diff = CODE_DIFF
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    steps: [{ index: 0, status: 'ok', tool: 'code.run' }],
    codeplane,
  })
}

/** 环境档：`deterministic-no-llm`（离线编排档）/ `no_model`（后端写 degraded=no_model）。 */
function envRun(mode: 'deterministic-no-llm' | 'no_model'): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    runtime_mode: 'deterministic',
    llm: mode === 'no_model' ? { degraded: 'no_model' } : null,
    steps: [{ index: 0, status: 'ok', tool: 'research.search' }],
    artifacts: [art({ id: `art_env_${mode}`, content: PLAIN_ARTIFACT_CONTENT })],
  })
}

/** 模型驱动档：`runtime_mode='react'` + 非空 `llm` + 一句真实结论。 */
function modelRun(): RunLike {
  return detail({
    run_id: RUN_ID,
    intent: INTENT,
    runtime_mode: 'react',
    llm: { final_answer: '本次分析已完成，线索质量整体良好。' },
    steps: [{ index: 0, status: 'ok', tool: 'research.search' }],
    artifacts: [art({ id: 'art_model', content: MODEL_ARTIFACT_CONTENT })],
  })
}

// --- 用例 ------------------------------------------------------------------- //

test.describe('INC39 结果层对话式完成态', () => {
  // ① 三固定按钮已消失 + 结果面板无自有输入框 + 续聊输入框唯一。
  test('① 三固定按钮已消失 · 结果面板无自有输入框 · 唯一续聊输入是 conv-followup', async ({
    page,
  }) => {
    await boot(page, { runs: [richRun()] })

    // 面板确已渲染（防止「因为页面是空的所以 count=0」的假绿）。
    await expect(page.getByTestId('result-layer')).toBeVisible()
    await expect(page.getByTestId('result-conclusions')).toBeVisible()

    // 三档固定按钮 + 段内续聊输入 + 写死快捷项 + 旧容器：全部不存在（各带阳性对照）。
    for (const id of [
      'result-action-agent',
      'result-action-self',
      'result-action-view-diff',
      'result-continue',
      'result-next-actions',
    ]) {
      await expectAbsentWithControl(page, id)
    }
    await expect(page.locator('[data-testid^="result-quick-"]')).toHaveCount(0)
    // 前缀选择器的阳性对照：注入一个 `result-quick-*` ⇒ 计数 1（证明该前缀断言真的可证伪，
    // 重新引入写死的快捷项即会红）。
    const dropQuick = await injectProbe(page, '[data-testid="result-layer"]', 'result-quick-probe')
    await expect(page.locator('[data-testid^="result-quick-"]')).toHaveCount(1)
    await dropQuick()
    await expect(page.locator('[data-testid^="result-quick-"]')).toHaveCount(0)

    // 续聊输入框**唯一**：中列（会话列）恰有 1 个 input/textarea，且就是 conv-followup-input。
    await expect(page.locator('#conv-followup-input')).toHaveCount(1)
    await expect(page.getByTestId('conv-followup')).toHaveCount(1)
    const convCol = page.getByTestId('workspace-col-conversation')
    await expect(convCol.locator('input, textarea')).toHaveCount(1)
    // 判别力对照：再注入一个 input ⇒ 计数变 2（证明「=1」这条断言真的会红）。
    const drop = await injectProbe(page, '[data-testid="workspace-col-conversation"]', 'probe-inp')
    await page.evaluate(() => {
      const el = document.createElement('input')
      el.setAttribute('data-ff-probe', 'inc39')
      document.querySelector('[data-testid="workspace-col-conversation"]')?.appendChild(el)
    })
    await expect(convCol.locator('input, textarea')).toHaveCount(2)
    await drop()
    await expect(convCol.locator('input, textarea')).toHaveCount(1)

    // 结果面板**内部**：0 个 input/textarea（段⑤ 不再有自有输入框）。
    await expect(page.getByTestId('result-layer').locator('input, textarea')).toHaveCount(0)
  })

  // ② 快捷操作 ≤3 且由任务类型决定（三组 testid 集合互不相同）。
  test('② 快捷操作 ≤3 且按任务类型派生：三组 testid 集合互不相同', async ({ page }) => {
    const sets: Record<string, string[]> = {}

    // —— 代码档 ——
    await boot(page, { runs: [codeRun(true)] })
    const box = page.getByTestId('result-contextual-actions')
    await expect(box).toHaveCount(1)
    let n = await box.locator('button').count()
    expect(n).toBeGreaterThan(0)
    expect(n).toBeLessThanOrEqual(3)
    sets.code = await box.locator('button').evaluateAll((els) =>
      els.map((e) => e.getAttribute('data-testid') ?? ''),
    )
    // 判别力对照：**注满到第 4 个按钮** ⇒ 计数 4 > 3（证明「≤3」这条断言真的会红）。
    // 去重收敛后代码档仅 2 条，故需注入 2 个探针才能越过阈值（注入 1 个只到 3，不会红）。
    const dropProbes: (() => Promise<void>)[] = []
    for (let i = 0; i < 4 - n; i++) {
      dropProbes.push(
        await injectProbe(
          page,
          '[data-testid="result-contextual-actions"]',
          `result-ctx-probe-${i}`,
        ),
      )
    }
    await expect(box.locator('button')).toHaveCount(4)
    expect(await box.locator('button').count()).toBeGreaterThan(3)
    for (const d of dropProbes) await d()
    await expect(box.locator('button')).toHaveCount(n)

    // —— 数据档 ——
    await boot(page, { runs: [dataRun()] })
    await expect(page.getByTestId('result-contextual-actions')).toHaveCount(1)
    n = await page.getByTestId('result-contextual-actions').locator('button').count()
    expect(n).toBeGreaterThan(0)
    expect(n).toBeLessThanOrEqual(3)
    sets.data = await page
      .getByTestId('result-contextual-actions')
      .locator('button')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-testid') ?? ''))

    // —— 知识档 ——
    await boot(page, { runs: [kbRun()] })
    await expect(page.getByTestId('result-contextual-actions')).toHaveCount(1)
    n = await page.getByTestId('result-contextual-actions').locator('button').count()
    expect(n).toBeGreaterThan(0)
    expect(n).toBeLessThanOrEqual(3)
    sets.knowledge = await page
      .getByTestId('result-contextual-actions')
      .locator('button')
      .evaluateAll((els) => els.map((e) => e.getAttribute('data-testid') ?? ''))

    // 三组集合**互不相同**（证明不是写死的同一套）。
    const j = (a: string[]) => [...a].sort().join(',')
    expect(j(sets.code)).not.toBe(j(sets.data))
    expect(j(sets.code)).not.toBe(j(sets.knowledge))
    expect(j(sets.data)).not.toBe(j(sets.knowledge))
    // 且各档确由预期 key 组成（机械命名对照）。
    // 代码档（去重收敛后）**恰 2 条**：`code-fix` + `code-diff`；不再有 `code-rerun`
    // （「重新运行」由段①/段④ 的专责入口提供，见 ③a）。`toEqual` 是**逐项相等**，
    // 多一条（例如把 rerun 加回来）即变红。
    expect(sets.code).toEqual(['result-ctx-code-fix', 'result-ctx-code-diff'])
    expect(sets.data).toEqual([
      'result-ctx-data-deep-dive',
      'result-ctx-data-chart',
      'result-ctx-data-export',
    ])
    expect(sets.knowledge).toEqual([
      'result-ctx-kb-follow-up',
      'result-ctx-kb-sources',
      'result-ctx-kb-summary',
    ])
  })

  // ③a Diff 红线（反）：diff.present=false ⇒ 面板内不存在任何 Diff 动作。
  test('③a Diff 红线（反）：diff.present=false ⇒ 无任何 Diff 动作', async ({ page }) => {
    await boot(page, { runs: [codeRun(false)] })
    const box = page.getByTestId('result-contextual-actions')
    await expect(box).toHaveCount(1)

    // 红线：无真实变更文本 ⇒ 段⑤ 不出现任何 Diff 动作（阳性对照证明该选择器真能命中）。
    await expectAbsentWithControl(page, 'result-ctx-code-diff', '[data-testid="result-layer"]')
    // 段⑤ **动作集合逐项相等** = 恰 `['result-ctx-code-fix']`：既证明「无 diff 动作」不是
    // 「整段不渲染」（还留 1 条），也证明**没有**任何「重新运行」快捷项混进段⑤。
    // ⚠️ 此处**绝不**写 `getByTestId('result-ctx-code-rerun').toHaveCount(0)` —— 该 testid 已随
    // 去重收敛从 src 删除，那么写是**永真的真空断言**（正是上一轮修掉的那类假绿）。
    expect(
      await box
        .locator('button')
        .evaluateAll((els) => els.map((e) => e.getAttribute('data-testid'))),
    ).toEqual(['result-ctx-code-fix'])
    // 段⑤ 内没有任何标签为「重新运行」的按钮（去重在段⑤ 侧的可证伪表现）。
    await expect(box.getByRole('button', { name: '重新运行', exact: true })).toHaveCount(0)

    // —— 去重红线（可证伪）：**全结果面板内标签恰为「重新运行」的可见按钮恰 1 个** ——
    // 旧行为下段⑤ 会再给一个（`result-ctx-code-rerun`）⇒ 计数 2 ⇒ 红。`count=1` 天生可证伪
    // （变 2 或变 0 都红），无需对已删 testid 做「不存在」断言。
    const rerun = page
      .getByTestId('result-layer')
      .getByRole('button', { name: '重新运行', exact: true })
    await expect(rerun).toHaveCount(1)
    await expect(rerun.first()).toBeVisible()
    await expect(rerun.first()).toHaveText('重新运行')
    // 本 payload（代码档 + 离线编排 `deterministic` + 无交付）下，rerun 由**段① 执行环境状态**
    // 的 `result-env-rerun` 专责提供 —— 段④ 无阻塞 ⇒ 不渲染，故此处**不是** `result-rerun`。
    // （实测：`result-rerun` 仅在有阻塞时出现；见报告 §9「存疑/纠正」。）
    await expect(rerun.first()).toHaveAttribute('data-testid', 'result-env-rerun')
    // 判别力对照：临时注入**同标签**按钮 ⇒ 计数变 2（证明「=1」这条断言真的会红，不是选择器失灵）。
    await page.evaluate(() => {
      const d = document.createElement('button')
      d.type = 'button'
      d.textContent = '重新运行'
      d.setAttribute('data-ff-probe', 'inc39')
      document.querySelector('[data-testid="result-layer"]')?.appendChild(d)
    })
    await expect(rerun).toHaveCount(2)
    await page.evaluate(() => {
      for (const el of Array.from(document.querySelectorAll('[data-ff-probe="inc39"]'))) el.remove()
    })
    await expect(rerun).toHaveCount(1)

    // 面板内也没有任何文案含「差异」的按钮（红线：连措辞都不该出现）。
    await expect(page.getByTestId('result-contextual-actions').getByText('差异')).toHaveCount(0)
    // 代码变更区块给诚实空态（证明「后端确实没有变更」）。
    await expect(page.getByTestId('code-diff-empty')).toBeVisible()
  })

  // ③b Diff 红线（正）：diff.present=true ⇒ 该动作出现且可见。
  test('③b Diff 红线（正）：diff.present=true ⇒ 查看差异出现且可见', async ({ page }) => {
    await boot(page, { runs: [codeRun(true)] })
    const diffBtn = page.getByTestId('result-ctx-code-diff')
    await expect(diffBtn).toHaveCount(1)
    await expect(diffBtn).toBeVisible()
    await expect(diffBtn).toHaveText('查看差异')
    // 无代码变更空态消失（与 ③a 形成双向对照：同一 payload，仅 diff 开关不同）。
    await expect(page.getByTestId('code-diff-empty')).toHaveCount(0)
  })

  // ④a 环境状态不伪装：离线编排档（deterministic）⇒ result-env-status 可见且**非** Agent 结论。
  test('④a 环境状态不伪装：deterministic ⇒ result-env-status 可见且不进 Agent 结论位', async ({
    page,
  }) => {
    await boot(page, { runs: [envRun('deterministic-no-llm')] })

    const env = page.getByTestId('result-env-status')
    await expect(env).toHaveCount(1)
    await expect(env).toBeVisible()
    await expect(env).toContainText('平台编排')
    await expect(env).toContainText('未连接模型服务')

    // 核心红线：`本次运行未启用模型驱动` **不得**出现在结果 Tab 子树内（尤其不得当成结论）。
    await expect(page.locator('#res-panel-result')).not.toContainText('本次运行未启用模型驱动')

    // 段② 是**诚实空态**（`result-headline-fallback`），不是降级文案；与真结论位互斥。
    const fallback = page.getByTestId('result-headline-fallback')
    await expect(fallback).toBeVisible()
    await expect(fallback).toHaveText('本次运行没有生成自然语言结论')
    expect((await fallback.textContent())?.trim()).not.toBe('本次运行未启用模型驱动')
    await expect(page.getByTestId('result-headline')).toHaveCount(0)

    // 段④ 不重复同一件事：env 档不产生 `result-degrade-note`。
    await expect(page.getByTestId('result-degrade-note')).toHaveCount(0)
  })

  // ④b 环境状态不伪装：llm.degraded='no_model' ⇒ 同上。
  test('④b 环境状态不伪装：llm.degraded=no_model ⇒ 同上口径', async ({ page }) => {
    await boot(page, { runs: [envRun('no_model')] })
    const env = page.getByTestId('result-env-status')
    await expect(env).toBeVisible()
    await expect(env).toContainText('平台编排')
    await expect(page.locator('#res-panel-result')).not.toContainText('本次运行未启用模型驱动')
    await expect(page.getByTestId('result-headline-fallback')).toHaveText(
      '本次运行没有生成自然语言结论',
    )
  })

  // ④c 反证：模型驱动档 ⇒ result-env-status **不存在**。
  test('④c 反证：模型驱动档（react + 真实结论）⇒ result-env-status 不存在', async ({ page }) => {
    await boot(page, { runs: [modelRun()] })
    // 确是真结论态（不是空态）——证明「无 env 条」不是面板整体没渲染。
    await expect(page.getByTestId('result-headline')).toHaveText(
      '本次分析已完成，线索质量整体良好。',
    )
    await expectAbsentWithControl(page, 'result-env-status', '[data-testid="result-layer"]')
  })

  // ⑤a continue 动作真发请求（POST /workspace/tasks 且带 continued_from_run_id）。
  test('⑤a continue 动作真发请求：POST /workspace/tasks 带 continued_from_run_id', async ({
    page,
  }) => {
    await boot(page, { runs: [kbRun()] })
    const btn = page.getByTestId('result-ctx-kb-follow-up')
    await expect(btn).toBeVisible()
    await btn.click()

    await expect.poll(() => rec.workspaceTasks.length).toBe(1)
    const call = rec.workspaceTasks[0]
    expect(call.method).toBe('POST')
    expect(call.path).toBe('/api/workspace/tasks')
    expect(call.body.intent).toBe(INSTRUCTION_FOLLOW_UP)
    expect(call.body.parent_run_id).toBe(RUN_ID)
    expect(call.body.context?.continued_from_run_id).toBe(RUN_ID)
  })

  // ⑤b export 动作真产生下载锚点（不是装饰按钮）。
  test('⑤b export 动作真下载：产生真实 blob 锚点且文件名正确', async ({ page }) => {
    await boot(page, { runs: [fallbackRun()] })
    await expect(page.getByTestId('result-ctx-fallback-export')).toBeVisible()

    // 前置：未点击时没有任何下载锚点。
    expect(await page.evaluate(() => (window.__ffDownloads ?? []).length)).toBe(0)
    await page.getByTestId('result-ctx-fallback-export').click()

    await expect.poll(() => page.evaluate(() => (window.__ffDownloads ?? []).length)).toBe(1)
    const downloads = await page.evaluate(() => window.__ffDownloads ?? [])
    expect(downloads[0].download).toBe(`run-${RUN_ID}-result.md`)
    expect((downloads[0].href ?? '').startsWith('blob:')).toBe(true)
  })

  // ⑤c sources 动作真切换证据 Tab（真实可见态变化）。
  test('⑤c sources 动作真切 Tab：点击后 #res-panel-evidence 可见', async ({ page }) => {
    await boot(page, { runs: [kbRun()] })
    // 前置：证据 Tabpanel 默认不可见。
    expect(await panelVisible(page, 'res-panel-evidence')).toBe(false)
    await page.getByTestId('result-ctx-kb-sources').click()
    await expect(page.locator('#res-panel-evidence')).toBeVisible()
  })

  // ⑥ 不空转：无特征产物 ⇒ result-contextual-actions 整段不进 DOM。
  test('⑥ 不空转：无特征产物 ⇒ result-contextual-actions 整段不进 DOM', async ({ page }) => {
    await boot(page, { runs: [emptyRun()] })
    // 面板确实渲染了（否则「无快捷动作」会被误读成「页面空白」）。
    await expect(page.getByTestId('result-layer')).toBeVisible()
    await expect(page.getByTestId('result-conclusions')).toBeVisible()
    // 诚实空态 `result-empty` 在段⑥ 的**默认折叠** `<details>` 内 ⇒ 只判「在 DOM」（不判可见，
    // 否则会把「段⑥ 折叠」的设计误判成失败）。
    await expect(page.getByTestId('result-empty')).toHaveCount(1)

    // 段⑤ wrapper（`ResultContextActions.tsx::<div className="res-ctx-region">`）**没有** data-testid，
    // 故只能用**真实 class 选择器**断言它整段不进 DOM（用 `getByTestId('result-ctx-region')` 会因该
    // testid 在 src 中根本不存在而**恒为 0（真空/假绿）**）。配判别力对照：注入一个 `.res-ctx-region`
    // ⇒ 计数 1，证明该选择器真能命中这个 wrapper。
    await expect(page.locator('.res-ctx-region')).toHaveCount(0)
    await page.evaluate(() => {
      const d = document.createElement('div')
      d.className = 'res-ctx-region'
      d.setAttribute('data-ff-probe', 'inc39')
      document.querySelector('[data-testid="result-layer"]')?.appendChild(d)
    })
    await expect(page.locator('.res-ctx-region')).toHaveCount(1)
    await page.evaluate(() => {
      for (const el of Array.from(document.querySelectorAll('[data-ff-probe="inc39"]'))) el.remove()
    })
    await expect(page.locator('.res-ctx-region')).toHaveCount(0)

    await expectAbsentWithControl(page, 'result-contextual-actions', '[data-testid="result-layer"]')
    // 无「无后续动作」话术。
    await expect(page.getByText('无后续动作')).toHaveCount(0)
  })

  // ⑦ 底部 follow-up：placeholder 逐字 + 真发请求（parent_run_id + 主产物指纹）。
  test('⑦ 底部 follow-up placeholder 逐字且真发请求（含 continued_from_artifact_ref）', async ({
    page,
  }) => {
    await boot(page, { runs: [richRun()] })

    const input = page.locator('#conv-followup-input')
    await expect(input).toHaveAttribute('placeholder', FOLLOWUP_PLACEHOLDER)
    // 判别力对照：去掉省略号（近似串）必须**不**等于实际值，证明逐字比较真的有效。
    expect(await input.getAttribute('placeholder')).not.toBe('继续告诉 AI 你想怎么处理')

    await input.fill(FOLLOWUP_TEXT)
    await page.getByTestId('conv-followup').getByRole('button').click()

    await expect.poll(() => rec.workspaceTasks.length).toBe(1)
    const call = rec.workspaceTasks[0]
    expect(call.method).toBe('POST')
    expect(call.path).toBe('/api/workspace/tasks')
    expect(call.body.intent).toBe(FOLLOWUP_TEXT)
    expect(call.body.parent_run_id).toBe(RUN_ID)
    expect(call.body.context?.continued_from_run_id).toBe(RUN_ID)
    // 既有 context 参数一个不丢（能力不减少）：主产物指纹仍随请求带上，且逐字等于产物 ref。
    expect(call.body.context?.continued_from_artifact_ref).toBe(PRIMARY_REF)
  })
})
