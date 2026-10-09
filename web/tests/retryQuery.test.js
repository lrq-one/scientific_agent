import assert from 'node:assert/strict'
import test from 'node:test'
import { retryQueryForFailedTask } from '../src/retryQuery.js'

const question = '统计 training_db 中 train_v3 的 fused-ring train 和全版本总数'

test('retry uses failed task original query, never a generic retry command', () => {
  assert.equal(retryQueryForFailedTask('failed', [{ id: 't1', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question }]), question)
})

test('legacy UI retry text recovers the preceding actual question', () => {
  assert.equal(retryQueryForFailedTask('failed',
    [{ id: 't1', status: 'failed' }, { id: 't2', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question },
     { role: 'user', task_id: 't2', content: '重新回答' }]), question)
})

test('retry never guesses another task when task-scoped user message is missing', () => {
  assert.equal(retryQueryForFailedTask('failed', [{ id: 't2', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question }]), '')
})

test('retry disabled for nonfailed tasks or legacy marker without original', () => {
  assert.equal(retryQueryForFailedTask('completed', [{ id: 't1', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question }]), '')
  assert.equal(retryQueryForFailedTask('failed', [{ id: 't2', status: 'failed' }],
    [{ role: 'user', task_id: 't2', content: '重新回答' }]), '')
})

test('retry does not use unrelated more recent user messages', () => {
  assert.equal(retryQueryForFailedTask('failed', [{ id: 't1', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question },
     { role: 'user', task_id: 't2', content: '不相关的另一项分析' }]), question)
})

test('retry selects the newest failed question, not an earlier failed one', () => {
  const previous = '统计 train_v3 的训练覆盖'
  const latest = '只统计 fused-ring 的两个数量'
  assert.equal(retryQueryForFailedTask('failed',
    [{ id: 't1', status: 'failed' }, { id: 't2', status: 'completed' }, { id: 't3', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: previous },
     { role: 'user', task_id: 't2', content: '比较 MAE' },
     { role: 'user', task_id: 't3', content: latest }]), latest)
})

test('old failed tasks must not become retry targets after a newer completed task', () => {
  assert.equal(retryQueryForFailedTask('failed',
    [{ id: 't1', status: 'failed' }, { id: 't2', status: 'completed' }],
    [{ role: 'user', task_id: 't1', content: question },
     { role: 'user', task_id: 't2', content: '之后的成功问题' }]), '')
})

test('when latest task fails, missing exact task-scoped query disables retry', () => {
  assert.equal(retryQueryForFailedTask('failed',
    [{ id: 't1', status: 'failed' }, { id: 't2', status: 'failed' }],
    [{ role: 'user', task_id: 't1', content: question }]), '')
})
