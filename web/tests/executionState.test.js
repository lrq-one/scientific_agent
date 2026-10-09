import test from 'node:test'
import assert from 'node:assert/strict'
import { hydrateConversationTasks, recordTaskEvent, taskStatusForConversation } from '../src/executionState.js'

test('A running does not put B in running state', () => {
  const tasks = {}, active = {}
  recordTaskEvent(tasks, active, 'A', 'FOLLOW_UP_TYPE', { task_id: 'task-A', message: 'started' })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'running')
  assert.equal(taskStatusForConversation(tasks, active, 'B'), 'idle')
  recordTaskEvent(tasks, active, 'B', 'FOLLOW_UP_TYPE', { task_id: 'task-B' })
  assert.equal(taskStatusForConversation(tasks, active, 'B'), 'running')
  recordTaskEvent(tasks, active, 'A', 'CANCELLED', { task_id: 'task-A' })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'cancelled')
  assert.equal(taskStatusForConversation(tasks, active, 'B'), 'running')
})

test('backend persisted status restores after refresh without starting an agent', () => {
  const tasks = {}, active = {}
  hydrateConversationTasks(tasks, active, 'A', [{ id: 'task-A', thread_id: 'thread-A', status: 'running' }])
  hydrateConversationTasks(tasks, active, 'B', [])
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'running')
  assert.equal(taskStatusForConversation(tasks, active, 'B'), 'idle')
  recordTaskEvent(tasks, active, 'A', 'FINAL_ANSWER', { task_id: 'task-A' })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'completed')
})

test('FINAL_ANSWER with inadequate Evidence does not appear as completed', () => {
  const tasks = {}, active = {}
  recordTaskEvent(tasks, active, 'A', 'FINAL_ANSWER', {
    task_id: 'failed-analysis',
    state: { quality_status: 'INSUFFICIENT_EVIDENCE',
             goal_coverage: { status: 'PARTIAL', missing_deliverables: ['csv_export'] } },
  })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'failed')
})

test('FINAL_ANSWER without evidence and with failed execution is failed', () => {
  const tasks = {}, active = {}
  recordTaskEvent(tasks, active, 'A', 'FINAL_ANSWER', {
    task_id: 'failed-analysis', state: { quality_status: 'EXECUTION_FAILED' },
  })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'failed')
})

test('supported final answer with all goals covered remains completed', () => {
  const tasks = {}, active = {}
  recordTaskEvent(tasks, active, 'A', 'FINAL_ANSWER', {
    task_id: 'successful-analysis',
    state: { quality_status: 'SUPPORTED_CONCLUSION',
             goal_coverage: { status: 'SATISFIED' } },
  })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'completed')
})

test('unsupported answer despite supported quality remains failed when coverage is partial', () => {
  const tasks = {}, active = {}
  recordTaskEvent(tasks, active, 'A', 'FINAL_ANSWER', {
    task_id: 'partial-analysis',
    state: { quality_status: 'SUPPORTED_CONCLUSION',
             goal_coverage: { status: 'PARTIAL' } },
  })
  assert.equal(taskStatusForConversation(tasks, active, 'A'), 'failed')
})
