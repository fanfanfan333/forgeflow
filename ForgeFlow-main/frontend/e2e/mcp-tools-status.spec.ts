import { expect, test } from '@playwright/test'
import { emptyApiResponse } from './apiFallback'

for (const connected of [true, false]) {
  test(`MCP status displays actual ${connected ? 'scoped tools' : 'unavailability'}`, async ({ page }) => {
    await page.addInitScript(() => {
      sessionStorage.setItem('forgeflow.jwt', 'ui-contract-only')
      sessionStorage.setItem('forgeflow.user', 'tools-user')
      sessionStorage.setItem('forgeflow.role', 'manager')
    })
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname
      const body = path === '/api/agents/mcp-status' ? {
        status: connected ? 'connected' : 'unavailable',
        tool_count: connected ? 1 : 0, updated_at: '2026-10-26T08:00:00Z',
        error: connected ? null : 'ConnectionError',
        tools: connected ? [{ id: 'mcp.internal.read_document', description: '读取已授权文档', read_only: true }] : [],
      } : emptyApiResponse(route.request().url())
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto('/tools')
    if (connected) {
      await expect(page.getByText('MCP · 已连接')).toBeVisible()
      await expect(page.getByRole('cell', { name: 'mcp.internal.read_document', exact: true })).toBeVisible()
      await expect(page.getByRole('cell', { name: '只读', exact: true })).toBeVisible()
    } else {
      await expect(page.getByText('MCP · 暂不可用')).toBeVisible()
      await expect(page.getByText('当前没有已连接且获得授权的工具。')).toBeVisible()
      await expect(page.getByRole('cell', { name: 'web_search', exact: true })).toHaveCount(0)
    }
  })
}
