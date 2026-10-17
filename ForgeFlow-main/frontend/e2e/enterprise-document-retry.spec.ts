import { emptyApiResponse } from './apiFallback'
import { expect, test } from '@playwright/test'

test('terminal verification failure replaces the streamed success draft', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('forgeflow.jwt', 'test-token')
    sessionStorage.setItem('forgeflow.role', 'admin')
    sessionStorage.setItem('forgeflow.user', 'test-user')
  })
  const finalText = '本次任务未完成验收：数字校验失败'
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const json = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (path.startsWith('/api/agents')) return json([])
    if (path === '/api/workspace/tasks') return json({ run_id: 'failed-check', thread_id: 'failed-thread', status: 'running' })
    if (path === '/api/runs/failed-check/events') {
      const events = [
        { type: 'run.final_answer', data: { text: '所有要求都已完成' }, seq: 1 },
        { type: 'run.failed', data: { status: 'failed' }, seq: 2 },
      ].map((event) => `data: ${JSON.stringify({ ...event, run_id: 'failed-check' })}\n\n`).join('')
      return route.fulfill({ contentType: 'text/event-stream', body: events + 'data: [DONE]\n\n' })
    }
    if (path === '/api/runs/failed-check') return json({
      run_id: 'failed-check', status: 'failed', outcome: 'failure', intent: '修改文件',
      steps: [], errors: ['数字校验失败'], artifacts: [], tool_invocations: [],
      llm: { final_answer: { text: finalText } }, detail_retained: true,
    })
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [], has_data: false }))
  })
  await page.goto('/')
  await page.getByTestId('chat-composer-input').fill('修改文件')
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('chat-agent-reply')).toContainText(finalText)
  await expect(page.getByTestId('chat-agent-reply')).not.toContainText('所有要求都已完成')
})

test('failed admission preserves the draft and file, retry submits the same resource', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('forgeflow.jwt', 'test-token')
    sessionStorage.setItem('forgeflow.role', 'admin')
    sessionStorage.setItem('forgeflow.user', 'test-user')
  })
  const submitted: Record<string, unknown>[] = []
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const json = (body: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    })
    if (path.startsWith('/api/agents')) return json([])
    if (path === '/api/metrics' || path === '/api/metrics/') return json({ has_data: false })
    if (path === '/api/cost/savings') return json({ has_data: false })
    if (path === '/api/workspace/tasks') {
      submitted.push(route.request().postDataJSON())
      if (submitted.length === 1) return json({ detail: 'Resource temporarily unavailable' }, 503)
      return json({ run_id: 'retry-run', thread_id: 'retry-thread', status: 'running' })
    }
    if (path === '/api/resources') return json({ total: 1, items: [{
      id: 'document-1', name: '报告.pptx', kind: 'file', status: 'parsed',
      locator: {}, summary: {}, detail: '',
    }] })
    if (path === '/api/documents/capabilities') return json({ matrix: {
      pptx: { editable: true, inplace: true, notes: '支持文本修改；保留版式和母版' },
    } })
    if (path === '/api/resources/limits') return json({
      max_bytes: 1048576, supported_extensions: ['.pptx'],
    })
    if (path === '/api/runs/retry-run') return json({
      run_id: 'retry-run', status: 'completed', outcome: 'success', steps: [],
      errors: [], artifacts: [], tool_invocations: [], llm: {}, detail_retained: false,
    })
    if (path.endsWith('/events')) return route.fulfill({
      status: 200, contentType: 'text/event-stream',
      body: 'event: run.completed\ndata: {"status":"completed"}\n\n',
    })
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [] }))
  })
  await page.goto('/')
  await page.getByTestId('chat-composer-attach').click()
  const file = page.getByTestId('resource-card').getByRole('checkbox')
  await file.check()
  await expect(page.getByTestId('resource-docx-only-note')).toContainText('支持文本修改')
  const input = page.getByTestId('chat-composer-input')
  await input.fill('修复报告中的文字')
  await page.getByTestId('chat-composer-send').click()
  await expect(input).toHaveValue('修复报告中的文字')
  await expect(file).toBeChecked()
  await expect(page.getByTestId('chat-composer-send')).toBeEnabled()
  await page.getByTestId('chat-composer-send').click()
  await expect(page.getByTestId('hero-attach-panel')).toHaveCount(0)
  expect(submitted).toHaveLength(2)
  expect(submitted[1]).toEqual(submitted[0])
  expect(submitted[1].context).toEqual({ resources: ['document-1'] })
})
