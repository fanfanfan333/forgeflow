/**
 * INC26 / T06 — 资源上传体验前端 e2e（AC-1 ~ AC-8 / AC-19 / AC-20）。
 *
 * 目标：在**真实构建产物**（`vite preview`）上用真实浏览器驱动 `ResourcePicker`，
 * 机械判定 INC26 新增/接通的四条交互：
 *
 *   1. 拖拽 / 多选 N 个文件 ⇒ N 行**独立**上传项（AC-1 / AC-2）；
 *   2. **上传前预检**：超限 / 坏扩展名的行**原地标失败**且**不发送任何请求**
 *      （以 `/api/resources/files` 的请求计数判定，AC-3）；合法文件仍正常上传（一个失败不连坐）；
 *   3. **预览**：表格逐字一致 + `truncated` 显示截断提示；文本逐字一致；非文件类诚实空态（AC-5 / AC-6）；
 *   4. **类型筛选**：`kind` 筛选后只返回该类型（AC-8）。
 *
 * 网络边界说明（分层诚实）：本 spec 在 `/api/**` 边界做 **route 拦截**（stub 后端），
 * 专注验证**前端**在给定后端响应下的真实交互——这正是 AC-1~AC-8 的落点（`docs/sop/INC26-DESIGN.md`
 * §9：AC-1/2/5/6/8 属 T02 前端）。后端语义（真实 multipart、413/400 诚实拒绝、
 * 单一事实源 limits）由 `tests/integration/test_inc26_upload_a_profile.py` 用真实 HTTP 覆盖。
 * 不新增 vitest；仅用既有 `frontend/e2e/`（playwright）。
 *
 * 引文纪律：一律 `文件名::符号名`（见 ResourcePicker.tsx::ResourcePicker）。
 */
import { test, expect } from '@testrelic/playwright-analytics/fixture'
import type { Page, Route } from '@playwright/test'

// --- 与后端单一事实源同形的最小夹具 ------------------------------------------ //
const LIMITS = {
  max_bytes: 64, // 故意很小，便于用「小文件」制造「超限」
  supported_extensions: ['.csv', '.tsv', '.txt', '.md', '.json'],
  note: '上限与类型白名单来自后端单一事实源，前端不得写死',
}

type StubResource = {
  id: string
  kind: string
  name: string
  status: string
  summary?: Record<string, unknown>
}

const FILE_A: StubResource = {
  id: 'res_file_a',
  kind: 'file',
  name: 'leads.csv',
  status: 'parsed',
  summary: { kind: 'file', rows: 3, columns: 2, chars: 24 },
}
const DB_B: StubResource = {
  id: 'res_db_b',
  kind: 'database',
  name: 'public.orders',
  status: 'registered',
  summary: { kind: 'database' },
}

/** 记录进入 `/api/resources/files` 的 POST 次数（预检判定用，AC-3）。 */
type Counters = { filePosts: number }
const counters: Counters = { filePosts: 0 }

async function stubApi(page: Page, opts?: { preview?: unknown; resources?: StubResource[] }) {
  const resources = opts?.resources ?? [FILE_A, DB_B]
  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()

    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/resources/limits') return json(LIMITS)

    if (path === '/api/resources' && method === 'GET') {
      const kind = url.searchParams.get('kind')
      const items = kind ? resources.filter((r) => r.kind === kind) : resources
      return json({ items })
    }

    if (path === '/api/resources/files' && method === 'POST') {
      counters.filePosts += 1
      const n = counters.filePosts
      return json({ ...FILE_A, id: `res_upload_${n}`, name: `upload_${n}.csv` }, 201)
    }

    if (/^\/api\/resources\/[^/]+\/preview$/.test(path)) {
      if (opts?.preview !== undefined) return json(opts.preview)
      return json({
        id: 'res_file_a',
        kind: 'file',
        available: true,
        format: 'table',
        columns: ['lead_id', 'amount'],
        rows: [
          ['1', '10'],
          ['2', '20'],
        ],
        content: '',
        truncated: true,
        note: '仅预览前 20 行',
      })
    }

    if (path === '/api/runs') return json({ items: [] })

    // 其余（session / 其它面板）一律给一个空对象，避免无关 4xx 噪声。
    return json({})
  })
}

/** 打开「文件」登记区（拖拽区 + limits 注记就位）。 */
async function openFileZone(page: Page) {
  await page.goto('/runs')
  await page.getByTestId('resource-add').click()
  await page.getByTestId('resource-kind-file').click()
  await expect(page.getByTestId('resource-dropzone')).toBeVisible()
  await expect(page.getByTestId('resource-limits-note')).toBeVisible()
}

/** 通过隐藏的 `<input type=file multiple>` 选中若干文件（触发 onChange）。 */
async function pickFiles(page: Page, files: { name: string; body: string }[]) {
  await page
    .locator('[data-testid=resource-dropzone] input[type=file]')
    .setInputFiles(
      files.map((f) => ({ name: f.name, mimeType: 'text/csv', buffer: Buffer.from(f.body) })),
    )
}

test.describe('INC26 资源上传体验', () => {
  test.beforeEach(async ({ page }) => {
    counters.filePosts = 0
    await stubApi(page)
  })

  // AC-1 / AC-2 —— 多选 N 个合法文件 ⇒ N 行独立上传项，全部成功。
  test('多选多个合法文件：N 行独立上传项且全部登记成功', async ({ page }) => {
    await openFileZone(page)
    await pickFiles(page, [
      { name: 'a.csv', body: 'x,y\n1,2\n' },
      { name: 'b.csv', body: 'x,y\n3,4\n' },
    ])
    const rows = page.getByTestId('resource-upload-row')
    await expect(rows).toHaveCount(2)
    // 每行独立：都到达「已解析」。
    await expect(page.getByTestId('resource-upload-status').filter({ hasText: '已解析' })).toHaveCount(2)
    expect(counters.filePosts).toBe(2)
  })

  // AC-2 —— 拖放（dataTransfer）与多选等价：N 行独立。
  test('拖拽投放多个文件：N 行独立上传项', async ({ page }) => {
    await openFileZone(page)
    const dt = await page.evaluateHandle(() => {
      const d = new DataTransfer()
      d.items.add(new File(['x,y\n1,2\n'], 'drop1.csv', { type: 'text/csv' }))
      d.items.add(new File(['x,y\n3,4\n'], 'drop2.csv', { type: 'text/csv' }))
      return d
    })
    await page.dispatchEvent('[data-testid=resource-dropzone]', 'drop', { dataTransfer: dt })
    const rows = page.getByTestId('resource-upload-row')
    await expect(rows).toHaveCount(2)
    // 等两行都到「已解析」再断言请求数，避免串行上传的竞态（两行都完成 ⇒ 两次 POST）。
    await expect(
      page.getByTestId('resource-upload-status').filter({ hasText: '已解析' }),
    ).toHaveCount(2)
    expect(counters.filePosts).toBe(2)
  })

  // AC-3 —— 上传前预检：超限 / 坏扩展名被拦，**不发送请求**；合法文件不受连坐。
  test('预检拦截超限与坏扩展名：原地失败且不发请求', async ({ page }) => {
    await openFileZone(page)
    await pickFiles(page, [
      { name: 'too_big.csv', body: 'x'.repeat(LIMITS.max_bytes + 10) }, // 超限
      { name: 'evil.exe', body: 'MZ' }, // 坏扩展名
      { name: 'ok.csv', body: 'x,y\n1,2\n' }, // 合法
    ])
    const rows = page.getByTestId('resource-upload-row')
    await expect(rows).toHaveCount(3)
    const statuses = page.getByTestId('resource-upload-status')
    // 两行被原地拦截（失败 + 逐字原因），一行成功。
    await expect(statuses.filter({ hasText: '失败' })).toHaveCount(2)
    await expect(statuses.filter({ hasText: '已解析' })).toHaveCount(1)
    // 只有合法文件发起了 POST（请求计数判定 —— 被拦的行**零请求**）。
    expect(counters.filePosts).toBe(1)
  })

  // AC-5 —— 表格预览逐字一致 + 截断提示。
  test('表格预览逐字渲染且显示截断提示', async ({ page }) => {
    await stubApi(page, {
      preview: {
        id: 'res_file_a',
        kind: 'file',
        available: true,
        format: 'table',
        columns: ['lead_id', 'amount'],
        rows: [
          ['1', '10'],
          ['2', '20'],
        ],
        content: '',
        truncated: true,
        note: '仅预览前 20 行',
      },
    })
    await openFileZone(page)
    // 表格类资源在列表里可见（stub 的 resources 同时含 file 与 database）。
    await page.getByRole('button', { name: '预览' }).first().click()
    const preview = page.getByTestId('resource-preview')
    await expect(preview).toBeVisible()
    // ⚠️ 竞态陷阱：原先用 `allTextContents()` + `expect(await …).toEqual(…)`。
    // `allTextContents()` **不做重试等待**，而 `expect(preview).toBeVisible()`
    // 只保证容器可见、**不保证 `tbody`/`thead` 已挂载** ⇒ 全量并发跑时约 17% 概率
    // 拿到空数组而假红（失败快照里 DOM 其实是对的）。改用**会重试**的匹配器。
    // 逐字语义不变：`toHaveText` 默认用 `textContent`（非 innerText），所以 `th` 的
    // CSS `text-transform: uppercase` 只是呈现层大写，源文本仍是夹具的列名 ——
    // AC-5 的「逐字一致」针对源文本。
    await expect(preview.locator('table tbody td')).toHaveText(['1', '10', '2', '20'])
    await expect(preview.locator('table thead th')).toHaveText(['lead_id', 'amount'])
    await expect(page.getByTestId('resource-preview-truncated')).toBeVisible()
  })

  // AC-6 —— 文本预览逐字一致。
  test('文本预览逐字渲染文件内容', async ({ page }) => {
    await stubApi(page, {
      preview: {
        id: 'res_file_a',
        kind: 'file',
        available: true,
        format: 'text',
        columns: [],
        rows: [],
        content: 'line one\nline two\n',
        truncated: false,
        note: '',
      },
    })
    await openFileZone(page)
    await page.getByRole('button', { name: '预览' }).first().click()
    const pre = page.locator('[data-testid=resource-preview] pre')
    await expect(pre).toBeVisible()
    // 逐字比对（textContent 不归一化空白；`toHaveText` 会折叠换行故不用）。
    expect(await pre.textContent()).toBe('line one\nline two\n')
  })

  // AC-6 —— 非文件类预览：诚实空态，不伪造内容。
  test('非文件类预览呈现诚实空态', async ({ page }) => {
    await stubApi(page, {
      resources: [DB_B],
      preview: {
        id: 'res_db_b',
        kind: 'database',
        available: false,
        format: 'none',
        columns: [],
        rows: [],
        content: '',
        truncated: false,
        note: 'database 资源不支持内容预览',
      },
    })
    await openFileZone(page)
    await page.getByRole('button', { name: '预览' }).first().click()
    const preview = page.getByTestId('resource-preview')
    await expect(preview).toContainText('不支持内容预览')
    await expect(preview.locator('table')).toHaveCount(0)
    await expect(page.getByTestId('resource-preview-truncated')).toHaveCount(0)
  })

  // AC-8 —— kind 筛选：返回项 kind 全部等于所选值。
  test('按类型筛选只返回该类型资源', async ({ page }) => {
    await openFileZone(page)
    await expect(page.getByTestId('resource-card')).toHaveCount(2)
    await page.getByTestId('resource-kind-filter').selectOption('file')
    const cards = page.getByTestId('resource-card')
    await expect(cards).toHaveCount(1)
    await expect(cards.first()).toContainText('文件')
    // 断言筛选确实作为查询参数发出（同源行为，不是纯前端过滤）。
    const kinds = await page.evaluate(() =>
      performance
        .getEntriesByType('resource')
        .map((e) => e.name)
        .filter((n) => n.includes('/api/resources')),
    )
    expect(kinds.some((n) => n.includes('kind=file'))).toBeTruthy()
  })

  // AC-19 —— 既有 testid 仍可寻址（回归钉子）。
  test('既有 data-testid 仍可寻址', async ({ page }) => {
    await page.goto('/runs')
    await expect(page.getByTestId('run-declare-table')).toHaveCount(1)
    await expect(page.getByTestId('run-declare-paths')).toHaveCount(1)
    await expect(page.getByTestId('resource-add')).toHaveCount(1)
    await expect(page.getByTestId('resource-kind-filter')).toHaveCount(1)
    await expect(page.getByTestId('resource-list')).toHaveCount(1)
  })
})
