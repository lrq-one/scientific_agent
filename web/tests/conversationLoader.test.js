import assert from 'node:assert/strict'
import test from 'node:test'

import { createConversationLoader } from '../src/conversationLoader.js'

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function fixture() {
  const conversations = new Map()
  const resources = new Map()
  const starts = []
  const commits = []
  let thread = 0
  const loader = createConversationLoader({
    fetchConversation: id => {
      const item = deferred(); conversations.set(id, item); return item.promise
    },
    fetchResources: id => {
      const item = deferred(); resources.set(id, item); return item.promise
    },
    createThreadId: () => `thread-${++thread}`,
  })
  const load = id => loader.load(id, {
    onStart: value => starts.push(value),
    onCommit: value => commits.push(value),
  })
  return { loader, conversations, resources, starts, commits, load }
}

test('A to B commits B and does not retain A while loading', async () => {
  const f = fixture()
  const a = f.load('A')
  assert.equal(f.starts.at(-1).id, 'A')
  f.conversations.get('A').resolve({ messages: [{ content: 'A' }] })
  f.resources.get('thread-1').resolve({ available_files: [] })
  assert.equal((await a).status, 'committed')

  const b = f.load('B')
  assert.equal(f.starts.at(-1).id, 'B')
  assert.equal(f.commits.at(-1).id, 'A')
  f.conversations.get('B').resolve({ messages: [{ content: 'B' }] })
  f.resources.get('thread-2').resolve({ available_files: [] })
  assert.equal((await b).status, 'committed')
  assert.equal(f.commits.at(-1).detail.messages[0].content, 'B')
})

test('rapid A to B to C only commits C even when A finishes last', async () => {
  const f = fixture()
  const a = f.load('A'); const b = f.load('B'); const c = f.load('C')
  f.conversations.get('C').resolve({ messages: [{ content: 'C' }] })
  f.resources.get('thread-3').resolve({ available_files: [] })
  assert.equal((await c).status, 'committed')
  f.conversations.get('B').resolve({ messages: [{ content: 'B' }] })
  f.resources.get('thread-2').resolve({ available_files: [] })
  f.conversations.get('A').resolve({ messages: [{ content: 'A' }] })
  f.resources.get('thread-1').resolve({ available_files: [] })
  assert.equal((await b).status, 'aborted')
  assert.equal((await a).status, 'aborted')
  assert.deepEqual(f.commits.map(item => item.id), ['C'])
})

test('history loading only performs detail and resource reads, never agent execution', async () => {
  let detailCalls = 0
  let resourceCalls = 0
  let agentCalls = 0
  const loader = createConversationLoader({
    fetchConversation: async () => { detailCalls += 1; return { messages: [] } },
    fetchResources: async () => { resourceCalls += 1; return {} },
    createThreadId: () => 'thread',
    executeAgent: () => { agentCalls += 1 },
  })
  await loader.load('A')
  assert.equal(detailCalls, 1)
  assert.equal(resourceCalls, 1)
  assert.equal(agentCalls, 0)
})

test('starting a new history load clears the previous visible conversation immediately', async () => {
  let visibleMessages = [{ content: 'previous conversation' }]
  let visibleTrace = { tools: ['previous tool'] }
  const detail = deferred()
  const resources = deferred()
  const loader = createConversationLoader({
    fetchConversation: () => detail.promise,
    fetchResources: () => resources.promise,
    createThreadId: () => 'thread',
  })

  const pending = loader.load('B', {
    onStart: () => { visibleMessages = []; visibleTrace = { tools: [] } },
    onCommit: ({ detail }) => { visibleMessages = detail.messages },
  })
  assert.deepEqual(visibleMessages, [])
  assert.deepEqual(visibleTrace, { tools: [] })
  detail.resolve({ messages: [{ content: 'next conversation' }], tasks: [] })
  resources.resolve({})
  await pending
  assert.deepEqual(visibleMessages, [{ content: 'next conversation' }])
})
