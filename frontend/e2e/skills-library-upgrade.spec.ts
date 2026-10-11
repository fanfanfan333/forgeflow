import { emptyApiResponse } from './apiFallback'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

async function setup(page: Page, role = 'manager') {
  await page.addInitScript(role => { sessionStorage.setItem('forgeflow.jwt', 'skill-test'); sessionStorage.setItem('forgeflow.role', role); sessionStorage.setItem('forgeflow.user', 'skill-user') }, role)
  const skills: Array<Record<string, unknown>> = []
  const candidates = [{ id: 'candidate-1', name: 'generic 技能候选', draft_spec: { prompt: '读取文件，并按实际需求修改内容。' }, status: 'draft' }]
  const requests: Record<string, unknown>[] = []
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url()); const path = url.pathname; const method = route.request().method()
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/resources/files') return json({id:'trial-resource',name:'sample.txt',kind:'file',status:'parsed',summary:{},locator:{},parsed:true},201)
    if (path === '/api/skills/instructions/try') return json({run_id:'trial-run',status:'running'})
    if (path === '/api/runs/trial-run') return json({run_id:'trial-run',status:'completed',outcome:'success',artifacts:[],steps:[]})
    if (path === '/api/skills/instructions' && method === 'POST') {
      const data = route.request().postDataJSON(); requests.push(data)
      const skill = { ...data, id: 'skill-' + requests.length, status: 'published', current_version: '1.0.0', owner: 'skill-user', domain: '文档处理', source: `---\nname: ${data.name}\n---\n\n${data.instructions}` }
      skills.push(skill); return json(skill, 201)
    }
    if (path === '/api/skills') { const q = url.searchParams.get('q') || ''; const items = skills.filter(s => String(s.name).includes(q)); return json({ total: items.length, items }) }
    if (path === '/api/skill-candidates') return json({ total: candidates.length, items: candidates })
    if (path === '/api/skill-candidates/candidate-1' && method === 'DELETE') { candidates.splice(0); return json({ deleted: true }) }
    const skill = skills.find(s => path.startsWith('/api/skills/' + s.id))
    if (skill && path.endsWith('/archive')) { skills.splice(skills.indexOf(skill), 1); return json({ deleted: true }) }
    if (skill && path.endsWith('/instructions')) {
      if (method === 'POST') Object.assign(skill, route.request().postDataJSON(), { current_version: '1.0.1' })
      return json(skill)
    }
    if (path === '/api/workspace/tasks') { requests.push(route.request().postDataJSON()); return json({ run_id: 'skill-run', session_id: 'skill-run', status: 'running' }) }
    if (path.startsWith('/api/runs/skill-run')) return json({ run_id: 'skill-run', status: 'completed', intent: '处理文件', outcome: 'success', steps: [], artifacts: [], errors: [], llm: { final_answer: { text: '已按技能执行。' } } })
    if (path === '/api/health') return json({ status: 'healthy' })
    if (path.startsWith('/api/agents')) return json([])
    return json(emptyApiResponse(route.request().url(), { total: 0, items: [], has_data: false }))
  })
  await page.goto('/skills')
  return { skills, requests }
}

async function trialSkill(page: Page) {
  await page.getByLabel('试运行文件').setInputFiles({name:'sample.txt',mimeType:'text/plain',buffer:Buffer.from('客户：  张三\n金额：  100万元\n')})
  await page.getByRole('button',{name:'试运行',exact:true}).click()
  await expect(page.getByText('试运行通过，可以保存。')).toBeVisible()
}

test('instructions can be created, read, edited and selected for a real task request', async ({ page }) => {
  const state = await setup(page)
  await expect(page.getByTestId('skills-library-page')).toBeVisible()
  await expect(page.getByTestId('skill-workspace')).toHaveCount(0)
  await page.getByRole('button', { name: '添加这个技能' }).click()
  const form = page.getByRole('dialog', { name: '新建指令技能' })
  await expect(form.getByLabel('使用指令')).toHaveValue(/文档清洗/)
  await expect(form.getByRole('button',{name:'保存技能'})).toBeDisabled()
  await trialSkill(page)
  await form.getByRole('button', { name: '保存技能' }).click()
  const card = page.getByTestId('instruction-skill-card')
  await expect(card).toContainText('文档清洗')
  expect(state.requests[0].instructions).toContain('不修改数字')
  await card.getByRole('button', { name: '使用说明' }).click()
  await expect(page.getByRole('dialog')).toContainText('什么时候使用')
  const download = page.waitForEvent('download'); await page.getByRole('button', { name: '导出 SKILL.md' }).click()
  expect((await download).suggestedFilename()).toBe('SKILL.md')
  await page.getByRole('button', { name: '编辑指令' }).click()
  await page.getByLabel('使用指令').fill('只清理多余空格，保留数字和原意，并返回修改后的文件。')
  await page.getByRole('button', { name: '保存技能' }).click()
  await card.getByRole('link', { name: '使用技能' }).click()
  await expect(page.getByTestId('selected-instruction-skill')).toContainText('文档清洗')
  await page.getByTestId('chat-composer-input').fill('处理这个文件')
  await page.getByTestId('chat-composer-input').press('Enter')
  await expect.poll(() => state.requests.length).toBe(2)
  expect(state.requests[1].context).toMatchObject({ skill_ids: ['skill-1'] })
})

test('skill and candidate deletion are available with confirmation', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: '添加这个技能' }).click(); await trialSkill(page); await page.getByRole('button', { name: '保存技能' }).click()
  await page.getByTestId('instruction-skill-card').getByRole('button', { name: '删除技能：文档清洗' }).click()
  await expect(page.getByTestId('confirm-dialog-cancel')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByTestId('confirm-dialog-ok')).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.getByTestId('confirm-dialog-cancel')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '删除技能：文档清洗' })).toBeFocused()
  await expect(page.getByTestId('instruction-skill-card')).toHaveCount(1)
  await page.getByRole('button', { name: '删除技能：文档清洗' }).click(); await page.getByTestId('confirm-dialog-ok').click()
  await expect(page.getByTestId('instruction-skill-card')).toHaveCount(0)
  await page.getByRole('button', { name: /待整理/ }).click()
  await page.locator('.candidate-review-row').getByRole('button', { name: /删除技能/ }).click(); await page.getByTestId('confirm-dialog-ok').click()
  await expect(page.getByRole('heading', { name: '暂时没有待整理的经验' })).toBeVisible()
})

test('mobile layout fits and readonly users do not see management actions', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await setup(page, 'viewer')
  await expect(page.getByRole('button', { name: '新建技能' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /待整理/ })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
})
