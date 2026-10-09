import { chromium } from 'playwright'
import { marked } from 'marked'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const output = path.join(root, 'reports/ui_acceptance_20261008')
const ids = process.argv.slice(2)
if (ids.length !== 3) throw new Error('Supply three real persisted conversation IDs')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const http = [], errors = [], checks = []
page.on('pageerror', e => errors.push(e.message))
page.on('response', r => { if (r.url().includes(':8000/')) http.push({ url: r.url(), method: r.request().method(), status: r.status() }) })
const inspect = async id => (await page.request.get(`http://127.0.0.1:8000/api/conversations/${id}`, { headers: { 'X-User-Id': 'demo-researcher' } })).json()
const ready = async () => {
  await page.locator('.composer textarea').waitFor()
  await page.locator('.history.active').waitFor()
  await page.locator('.history-loading').waitFor({ state: 'hidden' })
  await page.waitForTimeout(300)
}
const latestAnswer = async () => await page.locator('.message-row.assistant').count() ? page.locator('.message-row.assistant').last().innerText() : ''
const matchesAnswer = async text => {
  const expectedHtml = await page.evaluate(html => new DOMParser().parseFromString(html, 'text/html').body.innerHTML.trim(), marked.parse(text))
  const actualHtml = await page.locator('.message-row.assistant .markdown').last().innerHTML()
  return actualHtml.trim() === expectedHtml
}
try {
  await page.goto(`http://127.0.0.1:5173/c/${ids[0]}`); await ready()
  const expected = []
  for (const id of ids) {
    const d = await inspect(id)
    expected.push({ id, answer: d.messages.filter(m => m.role === 'assistant').at(-1)?.content || '',
      evidenceTaskIds: [...new Set(d.evidence.map(e => e.task_id))], taskIds: d.tasks.map(t => t.id) })
  }
  const postCount = http.filter(r => r.method === 'POST').length
  for (const id of [ids[1], ids[0]]) {
    const started = Date.now(), requestCount = http.length
    await page.locator(`.history[data-conversation-id="${id}"]`).click()
    const immediate = { activeId: await page.locator('.history.active').getAttribute('data-conversation-id'),
      loading: await page.locator('.history-loading').isVisible(), previousAnswerVisible: await page.locator('.message-row.assistant').count() > 0 }
    await ready()
    const answer = await latestAnswer()
    checks.push({ scenario: 'history switch', conversationId: id, immediate, latencyMs: Date.now()-started,
      requestCount: http.length-requestCount, correctAnswer: await matchesAnswer(expected.find(e => e.id === id).answer), frontendAnswer: answer,
      noExecution: http.filter(r => r.method === 'POST').length === postCount })
  }
  // No request mocking or deliberate API delays: this is the live UI race.
  for (const id of ids) await page.locator(`.history[data-conversation-id="${id}"]`).click()
  await ready()
  checks.push({ scenario: 'rapid A -> B -> C', activeId: await page.locator('.history.active').getAttribute('data-conversation-id'),
    correctAnswer: await matchesAnswer(expected[2].answer), noExecution: http.filter(r => r.method === 'POST').length === postCount })
  await page.screenshot({ path: path.join(output, '23-history-isolation.png'), fullPage: true })

  // Start and cancel a real user task through the existing Vue controls.
  const createdPromise = page.waitForResponse(r => r.request().method() === 'POST' && /:8000\/api\/conversations$/.test(r.url()))
  await page.locator('.new-task').click()
  const created = await (await createdPromise).json()
  await page.waitForURL(`http://127.0.0.1:5173/c/${created.id}`); await ready()
  await page.locator('.composer textarea').fill('比较 model_v1.csv 和 model_v2.csv，再检查 training_db 中 train_v3 的结构训练覆盖，整理关键证据。')
  await page.locator('.composer .send').click()
  let d, task
  const start = Date.now()
  while (Date.now()-start < 20000) {
    d = await inspect(created.id); task = d.tasks.at(-1)
    if (task?.status === 'running' && await page.locator('.composer .send').innerText() === '取消') break
    await page.waitForTimeout(200)
  }
  if (!task || task.status !== 'running') throw new Error('No live cancellable task')
  await page.locator('.composer .send').click()
  const cancelledAt = Date.now()
  while (Date.now()-cancelledAt < 15000) {
    d = await inspect(created.id); task = d.tasks.find(t => t.id === task.id)
    if (task.status === 'cancelled') break
    await page.waitForTimeout(300)
  }
  await page.waitForTimeout(1500)
  const after = await inspect(created.id)
  checks.push({ scenario: 'cooperative cancel', conversationId: created.id, taskId: task.id,
    threadId: task.thread_id, status: task.status, latencyMs: Date.now()-cancelledAt,
    postCancelToolStarts: after.events.filter(e => e.task_id === task.id && e.event_type === 'TOOL_STARTED' &&
      new Date(e.created_at).getTime() > cancelledAt).length,
    events: after.events.filter(e => e.task_id === task.id), noPermanentLoading: !(await page.locator('.composer .send').innerText()).includes('取消') })
  await page.reload(); await ready()
  checks.push({ scenario: 'cancelled history reload', status: after.tasks.at(-1).status,
    body: await page.locator('body').innerText(), pageErrors: [...errors] })
  await page.screenshot({ path: path.join(output, '26-cancellation.png'), fullPage: true })
  console.log(JSON.stringify(checks.map(({events, body, ...check}) => check)))
} finally {
  await writeFile(path.join(output, 'lifecycle.json'), JSON.stringify({ checks, http, errors }, null, 2))
  await browser.close()
}
