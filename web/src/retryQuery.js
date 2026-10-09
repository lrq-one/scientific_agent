/**
 * Resolve the exact user request to retry after a failed task.
 * Never turn a UI retry into a new semantic query such as "重新回答".
 * A missing task-scoped user message is ambiguous, so do not guess.
 */
export function retryQueryForFailedTask(status, tasks, messages) {
  if (status !== 'failed') return ''
  const failedTask = [...(tasks || [])].reverse().find(item => item.status === 'failed')
  if (!failedTask) return ''
  const users = (messages || []).filter(item => item.role === 'user')
  const original = users.findLast(item => item.task_id === failedTask.id)
  if (!original) return ''
  const text = String(original.content || '').trim()
  if (text !== '重新回答') return text
  // Backward compatibility for legacy attempts whose user query was only
  // the UI's old "重新回答" placeholder. Never reuse another conversation.
  const index = users.lastIndexOf(original)
  const previous = users.slice(0, index).reverse().find(item =>
    String(item.content || '').trim() && String(item.content).trim() !== '重新回答'
  )
  return previous ? String(previous.content).trim() : ''
}
