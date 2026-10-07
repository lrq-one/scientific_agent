const API = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
const headers = { 'X-User-Id': 'demo-researcher' }

async function jsonRequest(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
  })
  if (!response.ok) {
    let detail = `请求失败：${response.status}`
    try { detail = (await response.json()).detail || detail } catch { /* keep status */ }
    throw new Error(detail)
  }
  return response.status === 204 ? null : response.json()
}

export const createConversation = (title = '新建科研任务') => jsonRequest('/api/conversations', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
})
export const listConversations = () => jsonRequest('/api/conversations')
export const getConversation = (id, options = {}) => jsonRequest(`/api/conversations/${id}`, options)
export const getMessages = (id) => jsonRequest(`/api/conversations/${id}/messages`)
export const renameConversation = (id, title) => jsonRequest(`/api/conversations/${id}`, {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
})
export const deleteConversation = (id) => jsonRequest(`/api/conversations/${id}`, { method: 'DELETE' })
export const cancelTask = (conversationId, taskId) => jsonRequest(
  `/api/conversations/${conversationId}/tasks/${taskId}/cancel`, { method: 'POST' },
)
export async function downloadArtifact(id, filename) {
  const response = await fetch(`${API}/api/artifacts/${id}/download`, { headers })
  if (!response.ok) throw new Error(`下载失败：${response.status}`)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = filename; anchor.click()
  URL.revokeObjectURL(url)
}

export async function getResources(threadId, options = {}) {
  const response = await fetch(`${API}/api/resources?thread_id=${encodeURIComponent(threadId)}`, { ...options, headers: { ...headers, ...(options.headers || {}) } })
  if (!response.ok) throw new Error(`资源请求失败：${response.status}`)
  return response.json()
}

export async function uploadFile(threadId, file) {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch(`${API}/api/files/upload?thread_id=${encodeURIComponent(threadId)}`, { method: 'POST', headers, body })
  if (!response.ok) throw new Error((await response.json()).detail || '上传失败')
  return response.json()
}

async function consumeSSE(response, onEvent) {
  if (!response.ok) throw new Error(`请求失败：${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const event = block.match(/^event: (.+)$/m)?.[1]
      const data = block.match(/^data: (.+)$/m)?.[1]
      const id = block.match(/^id: (\d+)$/m)?.[1]
      if (event && data) onEvent(event, JSON.parse(data), id ? Number(id) : null)
    }
  }
}

export async function streamChat(payload, onEvent) {
  return consumeSSE(await fetch(`${API}/api/chat/stream`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }), onEvent)
}

export async function streamConversation(conversationId, payload, onEvent) {
  return consumeSSE(await fetch(`${API}/api/conversations/${conversationId}/chat/stream`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }), onEvent)
}

export async function reconnectTask(conversationId, taskId, afterId, onEvent, options = {}) {
  return consumeSSE(await fetch(`${API}/api/conversations/${conversationId}/tasks/${taskId}/events?after_id=${afterId}`, {
    headers, signal: options.signal,
  }), onEvent)
}

export async function resumeTask(payload, onEvent) {
  return consumeSSE(await fetch(`${API}/api/agent/resume`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }), onEvent)
}

