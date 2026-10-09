// Four explicitly authorized real UI conversations on the normal product.
// No fixture seeding, route interception, retry, or Frozen report writes.
import { chromium } from 'playwright'
import { mkdir, readFile, writeFile, access } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const folder = path.join(root, 'reports/phase4a_three_blockers_fix_20261009/ui')
const endpoint = 'http://127.0.0.1:8000', frontend = 'http://127.0.0.1:5173'
const headers = { 'X-User-Id': 'demo-researcher' }
const cases = [
  { id: 'D09', query: '只统计 training_db 中 train_v3 的 fused-ring train split 样本数和全版本 fused-ring 总数，分别列出。' },
  { id: 'D08', query: '用 training_db 的真实 predictions 对比 baseline-run、candidate-run 按结构分组的 MAE，并说明合成数据局限；将实际结果导出 CSV。' },
  { id: 'M02', query: '比较 predictions_baseline.csv 与 predictions_candidate.csv 的结构误差变化，再核查 training_db 的 train_v3 同类 train split 覆盖。先核对旧数据库列 topology，不存在时依据 schema 更新查询计划。所有来源是合成 fixture，不能宣称真实模型已训练。', files: ['predictions_baseline.csv', 'predictions_candidate.csv'] },
  { id: 'D06', query: '从 training_db 查 train_v2 所有 split 合计的结构数量，和 train split 分开，不能把 membership 统称训练。' },
]
await mkdir(folder, { recursive: true })
// Refuse the entire run if any previous authorized attempt already exists.
for (const c of cases) {
  try { await access(path.join(folder, `${c.id}.json`)); throw new Error(`No repeat allowed: ${c.id}`) }
  catch (e) { if (e.code !== 'ENOENT') throw e }
}
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true })
let stop = null
try {
  for (const c of cases) {
    if (stop) break
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true })
    const page = await context.newPage()
    const record = { case_id: c.id, frontend, endpoint, resource_adaptation: 'existing main database identities; no scientific fixture seeding',
      started_at: new Date().toISOString(), turns: [], http: [], page_errors: [] }
    page.on('pageerror', e => record.page_errors.push(e.message))
    page.on('response', response => {
      if (response.url().startsWith(endpoint)) record.http.push({ endpoint: response.url().slice(endpoint.length), method: response.request().method(), status: response.status() })
    })
    const destination = path.join(folder, `${c.id}.json`)
    try {
      const ready = await page.request.get(`${endpoint}/ready`)
      if (!ready.ok()) throw new Error(`Product not ready: HTTP ${ready.status()}`)
      await page.goto(frontend, { waitUntil: 'domcontentloaded', timeout: 60000 })
      await page.locator('.new-task').waitFor()
      const created = page.waitForResponse(r => r.url() === `${endpoint}/api/conversations` && r.request().method() === 'POST')
      await page.locator('.new-task').click()
      record.conversation_id = (await (await created).json()).id
      await page.waitForURL(`${frontend}/c/${record.conversation_id}`)
      await page.locator('.composer textarea').waitFor()
      await page.locator('.history-loading').waitFor({ state: 'hidden' })
      // Label only this newly created test conversation, so it is visible in 5173 history.
      const renamed = await page.request.patch(`${endpoint}/api/conversations/${record.conversation_id}`, {
        headers, data: { title: `[4A 验收 ${c.id}] ${c.query.slice(0, 36)}` } })
      if (!renamed.ok()) throw new Error(`Test-label HTTP ${renamed.status()}`)
      for (const name of c.files || []) {
        const uploaded = page.waitForResponse(r => r.url().includes('/api/files/upload') && r.request().method() === 'POST')
        await page.locator('input[type="file"]').setInputFiles(path.join(root, 'reports/phase4_v2_20261008/fixtures', name))
        if (!(await uploaded).ok()) throw new Error('Fixture upload failed')
      }
      const started = Date.now()
      await page.locator('.composer textarea').fill(c.query)
      await page.locator('.composer .send').click()
      const detail = async () => {
        const response = await page.request.get(`${endpoint}/api/conversations/${record.conversation_id}`, { headers })
        if (!response.ok()) throw new Error(`Conversation HTTP ${response.status()}`)
        return response.json()
      }
      let data, task
      while (Date.now() - started < 280000) {
        data = await detail(); task = data.tasks[0]
        if (task && ['completed', 'failed', 'cancelled', 'waiting_for_user'].includes(task.status)) break
        await page.waitForTimeout(600)
      }
      const turn = { query: c.query, task, latency_ms: Date.now() - started }
      if (!task || task.status === 'running') throw new Error('Driver timeout; no automatic resubmission')
      // Never invent an HITL answer or turn a single attempt into a retry.
      if (task.status === 'waiting_for_user') {
        turn.unanswered_hitl = true
        await page.locator('.hitl button').filter({ hasText: '取消任务' }).click()
        await page.waitForTimeout(600)
        data = await detail(); task = data.tasks.find(t => t.id === task.id); turn.task = task
      }
      for (const k of ['events', 'evidence', 'artifacts', 'messages', 'claims']) turn[k] = data[k].filter(item => item.task_id === task.id)
      await page.waitForTimeout(500)
      turn.frontend_answer = await page.locator(`.message-row.assistant[data-task-id="${task.id}"]`).allInnerTexts()
      turn.frontend_trace = await page.locator('.trace-block').allInnerTexts()
      turn.downloads = []
      for (let index = 0; index < await page.locator('.artifact').count(); index++) {
        const pending = page.waitForEvent('download', { timeout: 15000 })
        await page.locator('.artifact').nth(index).click()
        const download = await pending
        const filename = `${c.id}-${index}-${download.suggestedFilename()}`
        await download.saveAs(path.join(folder, filename))
        turn.downloads.push({ filename, failure: await download.failure() })
      }
      const posts = record.http.filter(r => r.method === 'POST').length
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.locator('.history-loading').waitFor({ state: 'hidden' })
      await page.waitForTimeout(500)
      turn.reload_extra_posts = record.http.filter(r => r.method === 'POST').length - posts
      turn.reload_answer = await page.locator(`.message-row.assistant[data-task-id="${task.id}"]`).allInnerTexts()
      record.turns.push(turn)
      record.cross_user_http_status = (await page.request.get(`${endpoint}/api/conversations/${record.conversation_id}`, { headers: { 'X-User-Id': 'acceptance-other-user' } })).status()
      await page.screenshot({ path: path.join(folder, `${c.id}.png`), fullPage: true })
    } catch (error) {
      record.infrastructure_error = error.message
      stop = error.message
      await page.screenshot({ path: path.join(folder, `${c.id}-error.png`), fullPage: true }).catch(() => {})
    } finally {
      record.finished_at = new Date().toISOString()
      await writeFile(destination, JSON.stringify(record, null, 2), { flag: 'wx' })
      await context.close()
    }
    const audit = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-m', 'evaluation.three_blockers_ui_audit', destination], { cwd: root, encoding: 'utf8' })
    console.log(audit.stdout || audit.stderr)
    if (audit.status !== 0) stop = 'Systemic deterministic defect: stop remaining conversations'
  }
} finally { await browser.close() }
if (stop) throw new Error(stop)
