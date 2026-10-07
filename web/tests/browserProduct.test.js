import test from 'node:test'
import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const appUrl = 'http://127.0.0.1:5173'
const apiUrl = 'http://127.0.0.1:8000'

test('real browser history switch and message lane stay scoped and aligned', async t => {
  const response = await fetch(`${apiUrl}/api/conversations`, { headers: { 'X-User-Id': 'demo-researcher' } })
  if (!response.ok) return t.skip('local product backend is unavailable')
  const conversations = (await response.json()).items
  if (conversations.length < 2) return t.skip('requires two persisted product conversations')
  const browser = await chromium.launch({ executablePath: edge, headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const agentPosts = []
    page.on('request', request => {
      if (request.method() === 'POST' && /chat\/stream|agent\/resume/.test(request.url())) agentPosts.push(request.url())
    })
    await page.goto(`${appUrl}/c/${conversations[0].id}`)
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    await page.locator(`.history.active`).waitFor()
    await page.locator('.history').filter({ has: page.locator('b') }).nth(1).click()
    await page.waitForURL(new RegExp(`/c/${conversations[1].id}$`))
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    assert.equal(agentPosts.length, 0, 'history switching must not start an agent')
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
    assert.equal(overflow, false)
    for (const width of [1100, 760]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false)
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(`${appUrl}/c/${conversations.find(item => item.message_count >= 2)?.id || conversations[0].id}`)
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    const geometry = await page.locator('.message-row').evaluateAll(rows => rows.map(row => {
      const bubble = row.querySelector('.user-bubble, .assistant-body')?.getBoundingClientRect()
      return { role: row.classList.contains('user') ? 'user' : 'assistant', left: bubble?.left, right: bubble?.right }
    }))
    const assistant = geometry.filter(item => item.role === 'assistant')
    if (assistant.length > 1) assert.ok(assistant.every(item => Math.abs(item.left - assistant[0].left) < 2))
    const users = geometry.filter(item => item.role === 'user')
    if (users.length > 1) assert.ok(users.every(item => Math.abs(item.right - users[0].right) < 2))
  } finally {
    await browser.close()
  }
})
