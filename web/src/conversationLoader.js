export function createConversationLoader({ fetchConversation, fetchResources, createThreadId }) {
  let sequence = 0
  let controller = null

  return {
    async load(id, callbacks = {}) {
      const requestId = ++sequence
      controller?.abort()
      controller = new AbortController()
      const signal = controller.signal
      const threadId = createThreadId()
      callbacks.onStart?.({ id, threadId, requestId })

      try {
        const [detail, resources] = await Promise.all([
          fetchConversation(id, { signal }),
          fetchResources(threadId, { signal }),
        ])
        if (signal.aborted) return { status: 'aborted', requestId }
        if (requestId !== sequence) return { status: 'stale', requestId }
        callbacks.onCommit?.({ id, threadId, detail, resources, requestId })
        return { status: 'committed', requestId }
      } catch (error) {
        if (signal.aborted || requestId !== sequence || error?.name === 'AbortError') {
          return { status: 'aborted', requestId }
        }
        callbacks.onError?.(error)
        throw error
      } finally {
        if (requestId === sequence) callbacks.onFinish?.({ id, requestId })
      }
    },
    cancel() {
      sequence += 1
      controller?.abort()
    },
  }
}
