const { chromium } = require('playwright')
const fs = require('fs')

const BASE = 'http://127.0.0.1:5199'

async function inspect(page, path, out) {
  const errs = []
  const onConsole = (m) => { if (m.type() === 'error') errs.push(m.text()) }
  const onPageErr = (e) => errs.push('PAGEERROR: ' + e.message)
  page.on('console', onConsole)
  page.on('pageerror', onPageErr)
  let resp = null
  try {
    resp = await page.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForTimeout(3500)
  } catch (e) {
    errs.push('GOTO: ' + (e && e.message))
  }
  const html = await page.content()
  const rec = {
    requested: path,
    finalUrl: page.url(),
    status: resp ? resp.status() : null,
    hasSidebar: /class="sidebar"/.test(html),
    hasTopbar: /class="topbar"/.test(html),
    hasHomeHero: html.includes('企业级 Multi-Agent 智能工作与技能资产平台'),
    hasLandingStage: /hero-stage/.test(html),
    activeNav: [],
    errors: errs.slice(0, 5),
  }
  try {
    rec.activeNav = await page.$$eval('.navlink.active', (els) => els.map((e) => e.textContent.trim()))
  } catch { /* ignore */ }
  page.off('console', onConsole)
  page.off('pageerror', onPageErr)
  out[path] = rec
}

;(async () => {
  const out = {}
  let browser
  try {
    browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--no-sandbox'] })
    const page = await browser.newPage()
    for (const p of ['/', '/welcome', '/console', '/console/runs', '/agents']) {
      await inspect(page, p, out)
    }
  } catch (e) {
    out._fatal = String((e && e.message) || e)
  } finally {
    if (browser) await browser.close().catch(() => {})
  }
  fs.writeFileSync('_verify_out.json', JSON.stringify(out, null, 2))
})()
