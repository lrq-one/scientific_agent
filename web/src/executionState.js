export const activeStatuses = new Set(['running', 'waiting_for_user', 'cancelling'])

export function taskStatusForConversation(taskExecutions, conversationActiveTask, conversationId) {
  const taskId = conversationActiveTask[conversationId]
  return taskId ? (taskExecutions[taskId]?.status || 'idle') : 'idle'
}

export function hydrateConversationTasks(taskExecutions, conversationActiveTask, conversationId, tasks) {
  const latest = [...tasks].reverse()[0]
  for (const task of tasks) {
    taskExecutions[task.id] = {
      conversation_id: conversationId, task_id: task.id, thread_id: task.thread_id,
      status: task.status, started_at: task.started_at, latest_event: null, progress: null,
    }
  }
  if (latest) conversationActiveTask[conversationId] = latest.id
  else delete conversationActiveTask[conversationId]
}

function finalAnswerTaskStatus(data) {
  const state = data.state || {}
  if (['EXECUTION_FAILED', 'INSUFFICIENT_EVIDENCE', 'CONFLICTING_EVIDENCE'].includes(state.quality_status)) return 'failed'
  if (state.quality_status === 'NO_DATA') return 'completed'
  if (['PARTIAL', 'UNSATISFIED', 'UNVERIFIABLE'].includes(state.goal_coverage?.status)) return 'failed'
  return 'completed'
}

export function recordTaskEvent(taskExecutions, conversationActiveTask, conversationId, type, data) {
  const taskId = data.task_id || conversationActiveTask[conversationId]
  if (!taskId) return
  const previous = taskExecutions[taskId] || {}
  const status = type === 'WAITING_FOR_USER' ? 'waiting_for_user'
    : type === 'CANCELLING' ? 'cancelling'
    : type === 'CANCELLED' ? 'cancelled'
    : type === 'FINAL_ANSWER' ? finalAnswerTaskStatus(data)
    : type === 'ERROR' ? 'failed' : 'running'
  taskExecutions[taskId] = {
    ...previous, conversation_id: conversationId, task_id: taskId,
    status, latest_event: type, progress: data.message || previous.progress || null,
  }
  conversationActiveTask[conversationId] = taskId
}
