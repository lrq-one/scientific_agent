import test from 'node:test'
import assert from 'node:assert/strict'
import { chromium } from 'playwright'

test('real browser follow-up reuses persisted evidence without SQL', async t => {
  const conversationId = process.env.P0_CONVERSATION_ID
  if (!conversationId) return t.skip('set P0_CONVERSATION_ID for the live product test')
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true,
  })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    await page.goto(`http://127.0.0.1:5173/c/${conversationId}`)
    await page.locator('.history-loading').waitFor({ state: 'hidden' })
    const before = await page.locator('.message-row.assistant').count()
    await page.locator('.composer textarea').fill('给我你得到的这些证据')
    await page.locator('.composer .send').click()
    await page.waitForFunction(expected => document.querySelectorAll('.message-row.assistant').length > expected, before)
    assert.match(await page.locator('.trace-block').first().innerText(), /EVIDENCE_EXPLANATION/)
    const answer = await page.locator('.message-row.assistant').last().innerText()
    assert.match(answer, /training_db|train_v3/)
    const response = await fetch(`http://127.0.0.1:8000/api/conversations/${conversationId}`, {
      headers: { 'X-User-Id': 'demo-researcher' },
    })
    assert.equal(response.status, 200)
    const detail = await response.json()
    const task = detail.tasks.at(-1)
    const events = detail.events.filter(item => item.task_id === task.id)
    const followup = events.find(item => item.event_type === 'FOLLOW_UP_TYPE')?.payload_json
    assert.equal(followup?.follow_up_type, 'EVIDENCE_EXPLANATION')
    assert.ok(followup?.previous_task_id)
    assert.ok(followup?.loaded_evidence_count > 0)
    assert.equal(followup?.new_tool_calls, 0)
    assert.ok(!events.some(item => ['TOOL_STARTED', 'TOOL_FINISHED'].includes(item.event_type)))
    await page.screenshot({ path: 'p0-followup-ui.png', fullPage: true })
    console.log(JSON.stringify({ conversationId, taskId: task.id, previousTaskId: followup.previous_task_id,
      loadedEvidenceCount: followup.loaded_evidence_count, newToolCalls: followup.new_tool_calls }))
  } finally {
    await browser.close()
  }
})
