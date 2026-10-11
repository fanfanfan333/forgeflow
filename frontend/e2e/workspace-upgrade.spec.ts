import { emptyApiResponse } from './apiFallback'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

type FixtureRun = { run_id: string; session_id: string; intent: string; status: string }

async function workspace(page: Page, running = false) {
  await page.addInitScript(() => {
    sessionStorage.setItem('forgeflow.jwt', 'workspace-test-token')
    sessionStorage.setItem('forgeflow.role', 'admin')
    sessionStorage.setItem('forgeflow.user', 'workspace-user')
  })
  const runs: FixtureRun[] = []
  const requests: Record<string, unknown>[] = []
  const reply = '## 处理结果\n\n**检查完成**。\n\n- 已整理结构\n- 已核对内容\n\n```python\nprint("ready")\n```'
  const detail = (run: FixtureRun) => ({
    ...run, thread_id: 'thread-' + run.run_id, outcome: run.status === 'completed' ? 'success' : run.status,
    steps: [], errors: [], artifacts: [], tool_invocations: [],
    created_at: '2026-10-07T08:00:00Z', completed_at: run.status === 'running' ? null : '2026-10-07T08:00:05Z',
    detail_retained: true, llm: { runtime_mode: 'react', final_answer: run.status === 'completed' ? { text: reply } : null },
  })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/health') return json({ status: 'ok', database: 'connected', graph: 'ready' })
    if (path.startsWith('/api/agents')) return json([])
    if (path === '/api/metrics' || path === '/api/metrics/' || path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/runs/model-status') return json({ provider: 'ollama', probed: false,
      models: [{ slot: 'strong', model: 'qwen3:8b', llm_type: 'chat-ollama' }] })
    if (path === '/api/workspace/tasks' && method === 'POST') {
      const input = route.request().postDataJSON()
      requests.push(input)
      const id = 'workspace-run-' + requests.length
      const sid = input.session_id || id
      runs.push({ run_id: id, session_id: sid, intent: input.intent, status: running ? 'running' : 'completed' })
      return json({ run_id: id, thread_id: 'thread-' + id, session_id: sid, status: 'running' })
    }
    if (path === '/api/workspace/sessions') {
      const ids = [...new Set(runs.map((run) => run.session_id))]
      return json({ total: ids.length, items: ids.map((id) => ({ session_id: id,
        title: runs.find((run) => run.session_id === id)?.intent || '新对话',
        run_count: runs.filter((run) => run.session_id === id).length, latest_status: runs.at(-1)?.status,
        created_at: '2026-10-07T08:00:00Z' })) })
    }
    if (path.startsWith('/api/workspace/sessions/')) {
      const id = decodeURIComponent(path.split('/').at(-1)!)
      if (method === 'DELETE') {
        const count = runs.filter(run => run.session_id === id).length
        for (let index = runs.length - 1; index >= 0; index--) if (runs[index].session_id === id) runs.splice(index, 1)
        return json({ session_id: id, deleted: count })
      }
      return json({ session_id: id, title: runs.find((run) => run.session_id === id)?.intent || '新对话',
        runs: runs.filter((run) => run.session_id === id).map(detail) })
    }
    if (path === '/api/runs') return json({ total: runs.length, items: runs.map(detail) })
    const run = runs.find((item) => path.startsWith('/api/runs/' + item.run_id))
    if (run && path.endsWith('/abort')) { run.status = 'aborted'; return json({ run_id: run.run_id, status: 'aborted' }) }
    if (run && path.endsWith('/events')) {
      if (run.status === 'running') return json({ detail: 'polling fixture' }, 503)
      const event = { run_id: run.run_id, type: run.status === 'aborted' ? 'run.aborted' : 'run.completed',
        data: { status: run.status }, seq: 1, ts: '2026-10-07T08:00:05Z' }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify(event)}\n\ndata: [DONE]\n\n` })
    }
    if (run) return json(detail(run))
    if (path === '/api/resources/limits') return json({ max_bytes: 5_242_880, supported_extensions: ['.docx', '.pptx', '.xlsx', '.pdf', '.txt'] })
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
  })
  await page.goto('/')
  await expect(page.getByTestId('chat-composer-input')).toBeEnabled()
  return { requests, runs }
}

test('multiline messages retain the same session and parent; reload and new chat work', async ({ page }) => {
  const state = await workspace(page)
  const input = page.getByTestId('chat-composer-input')
  await input.fill('检查这份方案')
  await input.press('Shift+Enter')
  await input.press('A')
  await expect(input).toHaveValue('检查这份方案\nA')
  await input.press('Enter')
  await expect(page.getByTestId('chat-agent-reply').getByRole('heading', { name: '处理结果' })).toBeVisible()
  await input.fill('继续完善')
  await expect(page.getByTestId('chat-composer-send')).toBeEnabled()
  await input.press('Enter')
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(2)
  expect(state.requests[1].session_id).toBe('workspace-run-1')
  expect(state.requests[1].parent_run_id).toBe('workspace-run-1')
  await page.reload()
  await expect(page.getByTestId('chat-user-message')).toHaveCount(2)
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(2)
  await page.getByTestId('sidebar-nav-new-chat').click()
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(0)
  await expect(page.getByTestId('chat-welcome')).toBeVisible()
  await input.fill('独立的新任务')
  await input.press('Enter')
  await expect.poll(() => state.requests.length).toBe(3)
  expect(state.requests[2].parent_run_id).toBeUndefined()
  await expect(page.getByTestId('chat-agent-reply')).toBeVisible()
  await page.getByTestId('sidebar-nav-recent-chat').click()
  await expect(page.getByTestId('conversations-page')).toBeVisible()
  await page.getByTestId('sidebar-nav-new-chat').click()
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(0)
  expect(state.requests[2].session_id).toBeUndefined()
})

test('approval-required responses explain the cause and preserve the draft', async ({ page }) => {
  await workspace(page)
  await page.route('**/api/workspace/tasks', route => route.fulfill({ status: 403, contentType: 'application/json',
    body: JSON.stringify({ detail: 'high-risk task requires human approval: high-risk action — human approval required (HITL)' }) }))
  await page.getByTestId('chat-composer-input').fill('删除所有文件')
  await page.getByTestId('chat-composer-input').press('Enter')
  await expect(page.locator('.chat-error')).toContainText('该操作需要人工审批')
  await expect(page.locator('.chat-error')).not.toContainText('无权限')
  await expect(page.getByTestId('chat-composer-input')).toHaveValue('删除所有文件')
})

test('recent conversations open chat; deletion cancels, reports failures, and clears active history', async ({ page }) => {
  const state = await workspace(page)
  await page.getByTestId('chat-composer-input').fill('待删除的对话')
  await page.getByTestId('chat-composer-input').press('Enter')
  await expect(page.getByTestId('chat-agent-reply')).toBeVisible()
  await page.getByTestId('sidebar-nav-recent-chat').click()
  await expect(page.getByTestId('conversations-page')).toBeVisible()
  const rows = page.locator('.conversation-row')
  await expect(rows).toHaveCount(1)
  await rows.getByRole('button', { name: '删除对话：待删除的对话' }).click()
  await page.getByTestId('confirm-dialog-cancel').click()
  expect(state.runs).toHaveLength(1)
  await page.route('**/api/workspace/sessions/workspace-run-1', async (route) => {
    if (route.request().method() === 'DELETE') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"删除暂不可用"}' })
    return route.fallback()
  })
  await rows.getByRole('button', { name: '删除对话：待删除的对话' }).click()
  await page.getByTestId('confirm-dialog-ok').click()
  await expect(page.getByTestId('confirm-dialog')).toContainText('503')
  expect(state.runs).toHaveLength(1)
  await page.unroute('**/api/workspace/sessions/workspace-run-1')
  await page.getByTestId('confirm-dialog-ok').click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(0)
  await expect(page.getByTestId('sidebar-conversation')).toHaveCount(0)
  await page.reload()
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(0)
})

test('stop uses the real abort endpoint and shows an aborted result', async ({ page }) => {
  const state = await workspace(page, true)
  await page.getByTestId('chat-composer-input').fill('执行一个较长的任务')
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('chat-composer-stop')).toBeVisible()
  await page.getByTestId('chat-composer-stop').click()
  await expect(page.getByTestId('chat-composer-stop')).toHaveCount(0)
  await expect(page.getByTestId('chat-exec-trace-summary')).toContainText('已中止')
  expect(state.runs[0].status).toBe('aborted')
})

test('sidebar search and deep links restore the conversation', async ({ page }) => {
  const state = await workspace(page)
  await page.getByTestId('chat-composer-input').fill('年度计划')
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('sidebar-conversation')).toHaveCount(1)
  await page.getByTestId('sidebar-nav-new-chat').click()
  await page.keyboard.press('Control+k')
  await expect(page.getByRole('textbox', { name: '搜索对话' })).toBeFocused()
  await page.getByRole('textbox', { name: '搜索对话' }).fill('不存在')
  await expect(page.getByTestId('sidebar-conversation')).toHaveCount(0)
  await page.getByRole('textbox', { name: '搜索对话' }).fill('年度')
  await page.getByTestId('sidebar-conversation').click()
  await expect(page).toHaveURL(/\/chat\/workspace-run-1$/)
  await expect(page.getByTestId('chat-user-message')).toContainText('年度计划')
  await page.getByTestId('chat-composer-input').fill('基于刚才的年度计划补充季度目标')
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('chat-user-message')).toHaveCount(2)
  await expect(page.getByTestId('chat-user-message').last()).toContainText('补充季度目标')
  expect(state.requests[1].session_id).toBe('workspace-run-1')
  expect(state.requests[1].parent_run_id).toBe('workspace-run-1')
})

test('desktop and mobile layouts fit the viewport and navigation is usable', async ({ page }) => {
  await workspace(page)
  await page.setViewportSize({ width: 1440, height: 960 })
  await expect(page.getByTestId('chat-welcome')).toBeVisible()
  await page.screenshot({ path: '../_audit/workspace-desktop.png', fullPage: true, animations: 'disabled' })
  await page.getByTestId('theme-toggle').click()
  await page.screenshot({ path: '../_audit/workspace-dark.png', fullPage: true, animations: 'disabled' })
  await page.getByTestId('theme-toggle').click()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await expect(page.getByTestId('chat-composer-input')).toBeVisible()
  await page.getByRole('button', { name: '切换导航' }).click()
  await expect(page.getByTestId('sidebar-nav-new-chat')).toBeVisible()
  await page.getByTestId('sidebar-nav-new-chat').click()
  await expect(page.getByRole('button', { name: '关闭导航' })).toHaveCount(0)
  await page.screenshot({ path: '../_audit/workspace-mobile.png', fullPage: true, animations: 'disabled' })
})

test('signing out clears rendered conversation and cached history', async ({ page }) => {
  await workspace(page)
  await page.getByTestId('chat-composer-input').fill('我的私人对话')
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('chat-agent-reply')).toBeVisible()
  await expect(page.getByTestId('sidebar-conversation')).toHaveCount(1)
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByTestId('chat-agent-message')).toHaveCount(0)
  await expect(page.getByTestId('sidebar-conversation')).toHaveCount(0)
  await expect(page.getByTestId('chat-composer-input')).toBeDisabled()
})
