import { emptyApiResponse } from './apiFallback'
import { expect, test } from '@playwright/test'

test('home attachments load their own styles, paginate, search and retain selection', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('forgeflow.jwt', 'test-token')
    sessionStorage.setItem('forgeflow.role', 'admin')
    sessionStorage.setItem('forgeflow.user', 'test-user')
  })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const json = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (path.startsWith('/api/agents')) return json([])
    if (path === '/api/resources') return json({ total: 25, items: Array.from({ length: 25 }, (_, i) => ({
      id: `file-${i}`, name: `file-${i}.txt`, kind: 'file', status: 'parsed', locator: {}, summary: { chars: 54 },
    })) })
    if (path.endsWith('/preview')) return json({ available: true, format: 'text', content: 'int main() { return 0; }', columns: [], rows: [], truncated: false })
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [], has_data: false }))
  })
  await page.goto('/')
  await page.getByTestId('chat-composer-attach').click()
  const panel = page.getByTestId('hero-attach-panel')
  await expect(panel.getByTestId('resource-card')).toHaveCount(12)
  await expect(panel.getByTestId('resource-list')).toHaveCSS('list-style-type', 'none')
  await expect(panel.getByTestId('resource-delete').first()).toBeHidden()
  await panel.screenshot({ path: '../_audit/attachment-panel.png' })
  await panel.getByRole('button', { name: '下一页' }).click()
  await panel.getByLabel('搜索资源').fill('file-24.txt')
  await expect(panel.getByTestId('resource-card')).toHaveCount(1)
  await panel.getByRole('checkbox').check()
  await expect(panel).toContainText('已选 1 项')
  await panel.getByRole('button', { name: '预览', exact: true }).click()
  await expect(page.getByTestId('resource-preview')).toContainText('return 0;')
  await page.getByTestId('resource-preview').getByRole('button', { name: '关闭' }).click()
  await panel.getByLabel('搜索资源').fill('')
  await expect(panel.getByTestId('resource-card')).toHaveCount(12)
  await expect(panel).toContainText('已选 1 项')
})
