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
