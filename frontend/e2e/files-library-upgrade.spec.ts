import { expect, test } from '@playwright/test'

async function setup(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('forgeflow.jwt', 'file-library-test')
    sessionStorage.setItem('forgeflow.role', 'manager')
    sessionStorage.setItem('forgeflow.user', 'file-user')
  })
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    const send = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (url.pathname === '/api/resources') return send({ items: Array.from({ length: 26 }, (_, i) => ({ id: `file-${i}`, name: `report-${i}.txt`, kind: 'file', status: 'parsed', summary: { chars: 42 } })) })
    if (url.pathname.endsWith('/preview')) return send({ available: true, format: 'text', content: '实际文档内容 42', columns: [], rows: [], truncated: false, note: '' })
    return send({ items: [], status: 'healthy' })
  })
  await page.goto('/files')
}

test('file library pages, searches and opens a modal without scrolling through the list', async ({ page }) => {
  await setup(page)
  await expect(page.getByTestId('resource-card')).toHaveCount(12)
  await page.getByRole('button', { name: '下一页' }).click()
  await expect(page.getByTestId('resource-card').first()).toContainText('report-12.txt')
  await page.getByRole('textbox', { name: '搜索资源' }).fill('report-25')
  await expect(page.getByTestId('resource-card')).toHaveCount(1)
  await page.getByRole('button', { name: '预览', exact: true }).click()
  const preview = page.getByRole('dialog', { name: '资源内容预览' })
  await expect(preview).toBeVisible()
  await expect(preview).toContainText('实际文档内容 42')
  await page.keyboard.press('Escape')
  await expect(preview).toHaveCount(0)
  await expect(page.getByTestId('sidebar-nav-files')).toHaveAttribute('aria-current', 'page')
  await expect(page.getByTestId('sidebar-nav-workspace')).not.toHaveAttribute('aria-current', 'page')
})

test('file library and modal fit a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await setup(page)
  await page.getByRole('button', { name: '预览', exact: true }).first().click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})
