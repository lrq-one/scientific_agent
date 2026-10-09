export const EXECUTION_STAGES = [
  { key: 'understanding', label: '任务理解', events: ['INTERACTION_RESOLVED', 'UNDERSTANDING_INTENT', 'INTENT_RESOLVED', 'SECURITY_DECISION', 'FOLLOW_UP_TYPE'] },
  { key: 'planning', label: '规划', events: ['PLAN_CREATED', 'PLAN_UPDATED', 'PLAN_REVISED', 'AGENT_DECISION', 'TOOL_CANDIDATES'] },
  { key: 'data_analysis', label: '数据分析', events: ['PLAN_STEP_STARTED', 'PLAN_STEP_FINISHED', 'TOOL_STARTED', 'TOOL_FINISHED', 'OBSERVATION_RECORDED'] },
  { key: 'evidence', label: '证据校验', events: ['EVIDENCE_ADDED', 'RECOVERY_DECISION', 'ARTIFACT_CREATED', 'WAITING_FOR_USER', 'HITL_RESUMED'] },
  { key: 'final_answer', label: '最终回答', events: ['FINAL_ANSWER', 'ERROR', 'CANCELLED'] },
]

const terminalEvents = new Set(['FINAL_ANSWER', 'ERROR', 'CANCELLED'])

export function stageForEvent(type) {
  return EXECUTION_STAGES.find(stage => stage.events.includes(type))?.key || null
}

export function buildStageSummary(events = [], status = 'idle') {
  const rows = events.map(item => typeof item === 'string' ? { type: item } : item)
  const seen = new Set(rows.map(item => stageForEvent(item.type)).filter(Boolean))
  const last = rows.length ? stageForEvent(rows[rows.length - 1].type) : null
  const terminal = rows.some(item => terminalEvents.has(item.type)) || ['completed', 'failed', 'cancelled'].includes(status)
  return EXECUTION_STAGES.map((stage, index) => {
    let state = 'pending'
    if (seen.has(stage.key)) state = stage.key === last && !terminal ? 'active' : 'completed'
    if (!seen.size && index === 0 && status === 'running') state = 'active'
    return { ...stage, state }
  })
}
