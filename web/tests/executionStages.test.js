import test from 'node:test'
import assert from 'node:assert/strict'
import { buildStageSummary, stageForEvent } from '../src/executionStages.js'

test('execution stage projection maps raw audit events', () => {
    assert.equal(stageForEvent('AGENT_DECISION'), 'planning')
    assert.equal(stageForEvent('TOOL_FINISHED'), 'data_analysis')
    assert.equal(stageForEvent('EVIDENCE_ADDED'), 'evidence')
    assert.equal(stageForEvent('UNRECOGNISED_EVENT'), null)
  })

test('execution stage projection shows five stages and keeps terminal status honest', () => {
    const summary = buildStageSummary([
      { type: 'INTENT_RESOLVED' }, { type: 'PLAN_CREATED' },
      { type: 'TOOL_FINISHED' }, { type: 'EVIDENCE_ADDED' }, { type: 'FINAL_ANSWER' },
    ], 'completed')
    assert.deepEqual(summary.map(item => item.key), ['understanding', 'planning', 'data_analysis', 'evidence', 'final_answer'])
    assert.equal(summary.every(item => item.state === 'completed'), true)
  })

test('execution stage projection does not infer progress from empty events', () => {
    assert.equal(buildStageSummary([], 'idle').every(item => item.state === 'pending'), true)
    assert.equal(buildStageSummary([], 'running')[0].state, 'active')
  })
