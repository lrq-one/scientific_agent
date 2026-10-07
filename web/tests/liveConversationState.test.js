import test from 'node:test'
import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const api = 'http://127.0.0.1:8000'
const headers = { 'X-User-Id': 'demo-researcher', 'Content-Type': 'application/json' }

test('real UI scopes A running state away from B and cancels A', async t => {
  if (process.env.P0_LIVE_RUNTIME !== '1') return t.skip('opt in with P0_LIVE_RUNTIME=1')
  const suffix = crypto.randomUUID().slice(0, 8)
  const create = async label => {
    const response = await fetch(`${api}/api/conversations`, {
      method: 'POST', headers, body: JSON.stringify({ title: `P0 UI ${label} ${suffix}` }),
    })
    assert.equal(response.status, 201)
    return response.json()
  }
  const a = await create('A')
  const b = await create('B')
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true,
  })
  let taskId = null
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    await page.goto(`http://127.0.0.1:5173/c/${a.id}`)
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    await page.locator('.composer textarea').fill('比较 model_v1.csv 和 model_v2.csv 的 RT 预测表现，并分析高误差分子主要集中在哪些结构类型。')
    await page.locator('.composer .send').click()
    await page.waitForFunction(() => document.querySelector('.composer .send')?.textContent.includes('取消执行'))
    for (let i = 0; i < 80; i++) {
      const detail = await fetch(`${api}/api/conversations/${a.id}`, { headers }).then(response => response.json())
      taskId = detail.tasks.at(-1)?.id
      if (taskId) break
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    assert.ok(taskId)
    await page.locator('.history').filter({ hasText: `P0 UI B ${suffix}` }).click()
    await page.waitForURL(new RegExp(`/c/${b.id}$`))
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    assert.match(await page.locator('.composer .send').innerText(), /开始分析/)
    const aStatus = await fetch(`${api}/api/conversations/${a.id}`, { headers }).then(response => response.json())
    assert.equal(aStatus.tasks.at(-1).status, 'running')
    await page.locator('.history').filter({ hasText: `P0 UI A ${suffix}` }).click()
    await page.waitForURL(new RegExp(`/c/${a.id}$`))
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    assert.match(await page.locator('.composer .send').innerText(), /取消执行/)
    await page.locator('.composer .send').click()
    for (let i = 0; i < 80; i++) {
      const detail = await fetch(`${api}/api/conversations/${a.id}`, { headers }).then(response => response.json())
      if (detail.tasks.at(-1)?.status === 'cancelled') break
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    const final = await fetch(`${api}/api/conversations/${a.id}`, { headers }).then(response => response.json())
    assert.equal(final.tasks.at(-1).status, 'cancelled')
    console.log(JSON.stringify({ conversationA: a.id, conversationB: b.id, taskA: taskId, taskAStatus: 'cancelled' }))
  } finally {
    if (taskId) {
      const detail = await fetch(`${api}/api/conversations/${a.id}`, { headers }).then(response => response.json())
      if (detail.tasks.at(-1)?.status === 'running') {
        await fetch(`${api}/api/conversations/${a.id}/tasks/${taskId}/cancel`, { method: 'POST', headers })
      }
    }
    await browser.close()
  }
})
