<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { marked } from 'marked'
import {
  cancelTask, createConversation, deleteConversation, downloadArtifact, getConversation, getReadiness,
  getResources, listConversations, reconnectTask, renameConversation, resumeTask, streamConversation, uploadFile,
} from './api'
import { createConversationLoader } from './conversationLoader'
import { activeStatuses, hydrateConversationTasks, recordTaskEvent, taskStatusForConversation } from './executionState'

const conversationId = ref('')
const conversations = ref([])
const threadId = ref(crypto.randomUUID())
const pendingThreadId = ref('')
const pendingTaskId = ref('')
const taskExecutions = reactive({})
const conversationActiveTask = reactive({})
const reconnectControllers = new Map()
const subscribedTasks = new Set()
const activeTaskId = computed(() => conversationActiveTask[conversationId.value] || '')
const currentStatus = computed(() => taskStatusForConversation(taskExecutions, conversationActiveTask, conversationId.value))
const busy = computed(() => currentStatus.value === 'running' || currentStatus.value === 'cancelling')
const cancelRequested = computed(() => currentStatus.value === 'cancelling')
const query = ref('')
const datasourceId = ref('')
const conversationLoading = ref(false)
const resources = ref({ available_files: [], authorized_datasources: [], available_scientific_models: [] })
const events = ref([])
const messages = ref([])
const tasks = ref([])
const pendingQuestion = ref('')
const answer = ref('')
const fileInput = ref(null)
const timeline = ref(null)
const traceOpen = ref(true)
const folds = ref({ plan: true, evidence: true, artifacts: true })
const trace = ref(emptyTrace())
const readiness = ref({ status: 'checking', ready: false, components: {} })
let sidebarTimer = null
let readinessTimer = null

const conversationLoader = createConversationLoader({
  fetchConversation: getConversation,
  fetchResources: getResources,
  createThreadId: () => crypto.randomUUID(),
})

const examples = [
  '比较 model_v1.csv 和 model_v2.csv 的 RT 预测表现，并分析高误差分子主要集中在哪些结构类型。',
  '分析含环分子的预测误差，并检查 fused-ring 是否存在训练覆盖不足。',
  '统计 training_db 中不同结构类型在 train_v3 的样本覆盖情况。',
]

const labels = {
  UNDERSTANDING_INTENT: '正在理解科研任务', INTENT_RESOLVED: '已识别任务类型',
  PLAN_CREATED: '已生成分析计划', TOOL_CANDIDATES: '已筛选候选工具',
  TOOL_STARTED: '正在调用分析工具', PLAN_STEP_STARTED: '计划步骤开始',
  PLAN_STEP_FINISHED: '计划步骤完成', TOOL_FINISHED: '工具执行完成',
  EVIDENCE_ADDED: '已获得新的科研证据', ARTIFACT_CREATED: '已生成结果产物',
  WAITING_FOR_USER: '等待用户补充信息', PLAN_REVISED: '已根据观察调整计划',
  RECOVERY_DECISION: '已判断恢复策略', INTERACTION_RESOLVED: '已理解本轮交互',
  CANCELLED: '已取消任务', FINAL_ANSWER: '分析完成', ERROR: '执行出错',
}

function emptyTrace() {
  return { interaction: null, intent: null, followup: null, skills: [], plan: [], tools: [], candidates: [], evidence: [], claims: [], sql: null, artifacts: [], waiting: '' }
}

const resourceCount = computed(() => resources.value.available_files.length + resources.value.authorized_datasources.length + resources.value.available_scientific_models.length)

const groupedConversations = computed(() => {
  const groups = { '今天': [], '昨天': [], '更早': [] }
  const now = new Date(); const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  for (const item of conversations.value) {
    const time = new Date(item.updated_at)
    const days = Math.floor((today - new Date(time.getFullYear(), time.getMonth(), time.getDate())) / 86400000)
    groups[days <= 0 ? '今天' : days === 1 ? '昨天' : '更早'].push(item)
  }
  return groups
})

async function refreshResources() {
  resources.value = await getResources(threadId.value)
  if (!datasourceId.value && resources.value.authorized_datasources.length) datasourceId.value = resources.value.authorized_datasources[0]
}

async function refreshConversations() {
  conversations.value = (await listConversations()).items
}

async function refreshReadiness() {
  try {
    readiness.value = await getReadiness()
  } catch {
    readiness.value = { status: 'unavailable', ready: false, components: {} }
  }
}

function sidebarStatus(item) {
  const local = taskStatusForConversation(taskExecutions, conversationActiveTask, item.id)
  if (['completed', 'failed', 'cancelled'].includes(item.latest_task_status) &&
      ['running', 'cancelling'].includes(local) && !item.active_task_id) return item.latest_task_status
  return local === 'idle' ? (item.latest_task_status || 'idle') : local
}

const statusLabels = {
  idle: '空闲', running: '运行中', waiting_for_user: '等待补充', cancelling: '正在取消',
  cancelled: '已取消', completed: '已完成', failed: '失败',
}

function subscribePersistedTask(id, task, detail) {
  if (!task || task.status !== 'running' || subscribedTasks.has(task.id)) return
  subscribedTasks.add(task.id)
  const controller = new AbortController()
  reconnectControllers.set(task.id, controller)
  const afterId = Math.max(0, ...(detail.events || []).filter(item => item.task_id === task.id).map(item => item.id))
  reconnectTask(id, task.id, afterId, (type, data) => receive(id, type, data), { signal: controller.signal })
    .catch(error => { if (error.name !== 'AbortError') console.error('task event reconnect failed', error) })
    .finally(() => { reconnectControllers.delete(task.id); subscribedTasks.delete(task.id); refreshConversations().catch(() => {}) })
}

function eventsForTask(taskId, detail) {
  return (detail.events || []).filter(item => item.task_id === taskId)
}

function applyPersistedTrace(task, detail) {
  const fresh = emptyTrace()
  fresh.intent = task.intent_json && Object.keys(task.intent_json).length ? task.intent_json : null
  fresh.skills = task.selected_skills_json || []
  for (const row of eventsForTask(task.id, detail)) {
    const data = row.payload_json || {}
    if (row.event_type === 'INTERACTION_RESOLVED') fresh.interaction = data
    if (row.event_type === 'FOLLOW_UP_TYPE') fresh.followup = data
    if (row.event_type === 'PLAN_CREATED') fresh.plan = data.plan || []
    if (row.event_type === 'PLAN_REVISED') fresh.plan = data.revised_plan || fresh.plan
    if (row.event_type === 'TOOL_CANDIDATES') fresh.candidates = data.candidate_tools || []
    if (row.event_type === 'TOOL_STARTED') fresh.tools.push({ tool: data.tool, status: 'running' })
    if (row.event_type === 'TOOL_FINISHED') {
      if (data.tool === 'text_to_sql') fresh.sql = data.result?.data || null
      const tool = [...fresh.tools].reverse().find(item => item.tool === data.tool && item.status === 'running')
      if (tool) tool.status = data.result?.success === false ? 'failed' : 'completed'
    }
    if (row.event_type === 'WAITING_FOR_USER') fresh.waiting = data.question || ''
  }
  fresh.evidence = (detail.evidence || []).filter(item => item.task_id === task.id)
  fresh.claims = (detail.claims || []).filter(item => item.task_id === task.id)
  fresh.artifacts = (detail.artifacts || []).filter(item => item.task_id === task.id)
  trace.value = fresh
  events.value = eventsForTask(task.id, detail).map(row => ({
    type: row.event_type, text: labels[row.event_type] || row.event_type,
    detail: row.payload_json?.message || '', time: new Date(row.created_at).toLocaleTimeString(),
  }))
}

async function loadConversation(id, push = true) {
  return conversationLoader.load(id, {
    onStart: ({ threadId: nextThreadId }) => {
      conversationId.value = id
      threadId.value = nextThreadId
      conversationLoading.value = true
      messages.value = []
      tasks.value = []
      events.value = []
      trace.value = emptyTrace()
      pendingQuestion.value = ''
      pendingThreadId.value = ''
      pendingTaskId.value = ''
      if (push && location.pathname !== `/c/${id}`) history.pushState({ conversationId: id }, '', `/c/${id}`)
    },
    onCommit: ({ detail, resources: loadedResources }) => {
      messages.value = detail.messages || []
      tasks.value = detail.tasks || []
      hydrateConversationTasks(taskExecutions, conversationActiveTask, id, tasks.value)
      const waitingTask = [...tasks.value].reverse().find(item => item.status === 'waiting_for_user')
      if (tasks.value.length) applyPersistedTrace(waitingTask || tasks.value[tasks.value.length - 1], detail)
      resources.value = loadedResources
      if (!datasourceId.value && resources.value.authorized_datasources.length) datasourceId.value = resources.value.authorized_datasources[0]
      if (waitingTask) {
        pendingThreadId.value = waitingTask.thread_id; pendingTaskId.value = waitingTask.id
        pendingQuestion.value = trace.value.waiting
      }
      subscribePersistedTask(id, [...tasks.value].reverse().find(item => item.status === 'running'), detail)
    },
    onError: error => {
      messages.value = [{ role: 'error', content: error.message }]
    },
    onFinish: () => { conversationLoading.value = false },
  })
}

async function newTask() {
  const created = await createConversation()
  await refreshConversations()
  await loadConversation(created.id)
  query.value = ''
}

async function renameItem(item) {
  const title = window.prompt('输入新的对话标题', item.title)
  if (!title?.trim()) return
  await renameConversation(item.id, title.trim())
  await refreshConversations()
}

async function removeItem(item) {
  if (!window.confirm(`确认删除“${item.title}”？`)) return
  await deleteConversation(item.id)
  await refreshConversations()
  if (item.id === conversationId.value) {
    if (conversations.value.length) await loadConversation(conversations.value[0].id)
    else await newTask()
  }
}

function receive(sourceConversationId, type, data) {
  recordTaskEvent(taskExecutions, conversationActiveTask, sourceConversationId, type, data)
  if (type === 'FINAL_ANSWER' || type === 'CANCELLED' || type === 'ERROR') refreshConversations().catch(() => {})
  if (sourceConversationId !== conversationId.value || conversationLoading.value) return
  events.value.push({ type, text: labels[type] || type, detail: data.message, time: new Date().toLocaleTimeString() })
  if (type === 'INTERACTION_RESOLVED') trace.value.interaction = data
  if (type === 'INTENT_RESOLVED') { trace.value.intent = data.intent; trace.value.skills = data.selected_skills || [] }
  if (type === 'FOLLOW_UP_TYPE') trace.value.followup = data
  if (type === 'PLAN_CREATED') trace.value.plan = data.plan || []
  if (type === 'PLAN_REVISED') trace.value.plan = data.revised_plan || []
  if (type === 'TOOL_CANDIDATES') trace.value.candidates = data.candidate_tools || []
  if (type === 'PLAN_STEP_STARTED' || type === 'PLAN_STEP_FINISHED') {
    const step = trace.value.plan.find(item => item.step_id === data.step_id); if (step) step.status = data.status
  }
  if (type === 'TOOL_STARTED') trace.value.tools.push({ tool: data.tool, status: 'running' })
  if (type === 'TOOL_FINISHED') {
    if (data.tool === 'text_to_sql') trace.value.sql = data.result?.data || null
    const item = [...trace.value.tools].reverse().find(tool => tool.tool === data.tool && tool.status === 'running')
    if (item) item.status = data.result?.success === false ? 'failed' : 'completed'
    else trace.value.tools.push({ tool: data.tool, status: 'completed' })
  }
  if (type === 'EVIDENCE_ADDED') trace.value.evidence.push(data.evidence)
  if (type === 'FINAL_ANSWER') trace.value.claims = data.state?.claims || []
  if (type === 'ARTIFACT_CREATED') trace.value.artifacts.push(data.artifact)
  if (type === 'FINAL_ANSWER') messages.value.push({ role: 'assistant', content: data.answer, task_id: data.task_id })
  if (type === 'WAITING_FOR_USER') { pendingQuestion.value = data.question; trace.value.waiting = data.question; pendingTaskId.value = data.task_id || '' }
  if (type === 'ERROR') messages.value.push({ role: 'error', content: data.error || data.message, task_id: data.task_id })
  if (type === 'CANCELLED') {
    messages.value.push({ role: 'system', content: '任务已取消。', task_id: data.task_id })
    pendingQuestion.value = ''
  }
  nextTick(() => timeline.value?.scrollTo({ top: timeline.value.scrollHeight, behavior: 'smooth' }))
}

async function primaryAction() {
  if (busy.value) return cancelRunning()
  if (!query.value.trim() && currentStatus.value === 'failed' && retryableFailedQuery.value) {
    query.value = retryableFailedQuery.value
  }
  return send()
}

async function send() {
  const text = query.value.trim()
  if (!text || busy.value || !conversationId.value) return
  const sourceConversationId = conversationId.value
  const activeThread = threadId.value
  pendingThreadId.value = activeThread
  const provisionalId = `pending:${sourceConversationId}`
  let terminalSeen = false
  taskExecutions[provisionalId] = { conversation_id: sourceConversationId, task_id: provisionalId, thread_id: activeThread, status: 'running' }
  conversationActiveTask[sourceConversationId] = provisionalId
  messages.value.push({ role: 'user', content: text })
  query.value = ''; events.value = []; trace.value = emptyTrace(); pendingQuestion.value = ''
  try {
    await streamConversation(sourceConversationId, {
      query: text, thread_id: activeThread, datasource_id: datasourceId.value || null,
    }, (type, data) => {
      if (data.task_id && conversationActiveTask[sourceConversationId] === provisionalId) {
        delete taskExecutions[provisionalId]
      }
      if (data.task_id) subscribedTasks.add(data.task_id)
      if (['FINAL_ANSWER', 'CANCELLED', 'ERROR', 'WAITING_FOR_USER'].includes(type)) {
        terminalSeen = true
        if (data.task_id) subscribedTasks.delete(data.task_id)
      }
      receive(sourceConversationId, type, data)
    })
    if (!terminalSeen) {
      const detail = await getConversation(sourceConversationId)
      subscribedTasks.delete(conversationActiveTask[sourceConversationId])
      subscribePersistedTask(sourceConversationId, [...(detail.tasks || [])].reverse().find(item => item.status === 'running'), detail)
    }
    await refreshConversations()
  } catch (error) {
    const detail = await getConversation(sourceConversationId).catch(() => null)
    const running = [...(detail?.tasks || [])].reverse().find(item => item.status === 'running')
    if (running) { subscribedTasks.delete(running.id); subscribePersistedTask(sourceConversationId, running, detail) }
    else receive(sourceConversationId, 'ERROR', { error: error.message, task_id: conversationActiveTask[sourceConversationId] })
  }
  finally {
    if (conversationActiveTask[sourceConversationId] === provisionalId) recordTaskEvent(taskExecutions, conversationActiveTask, sourceConversationId, 'ERROR', { task_id: provisionalId })
    if (sourceConversationId === conversationId.value) {
      if (!pendingQuestion.value) { threadId.value = crypto.randomUUID(); pendingThreadId.value = ''; pendingTaskId.value = '' }
      await refreshResources()
    }
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    await uploadFile(threadId.value, file); await refreshResources()
    events.value.push({ type: 'TOOL_FINISHED', text: '文件上传完成', detail: file.name, time: new Date().toLocaleTimeString() })
  } catch (error) { receive(conversationId.value, 'ERROR', { error: error.message }) }
  event.target.value = ''
}

async function resume() {
  if (!answer.value.trim()) return
  const sourceConversationId = conversationId.value
  const resumeTaskId = pendingTaskId.value
  const resumeThreadId = pendingThreadId.value || threadId.value
  recordTaskEvent(taskExecutions, conversationActiveTask, sourceConversationId, 'RESUMED', { task_id: resumeTaskId })
  const supplied = answer.value; answer.value = ''; pendingQuestion.value = ''
  try {
    await resumeTask({ thread_id: resumeThreadId, answer: supplied, conversation_id: sourceConversationId, task_id: resumeTaskId },
      (type, data) => receive(sourceConversationId, type, data))
    await refreshConversations()
    if (sourceConversationId === conversationId.value && !pendingQuestion.value) { pendingThreadId.value = ''; pendingTaskId.value = ''; threadId.value = crypto.randomUUID() }
  }
  catch (error) {
    receive(sourceConversationId, 'ERROR', { error: error.message, task_id: resumeTaskId })
    if (sourceConversationId === conversationId.value) await loadConversation(sourceConversationId, false)
  }
}

async function cancelRunning() {
  const sourceConversationId = conversationId.value
  const taskId = activeTaskId.value
  if (!busy.value || !taskId || taskId.startsWith('pending:') || cancelRequested.value) return
  recordTaskEvent(taskExecutions, conversationActiveTask, sourceConversationId, 'CANCELLING', { task_id: taskId })
  try { await cancelTask(sourceConversationId, taskId) }
  catch (error) { await loadConversation(sourceConversationId, false); receive(sourceConversationId, 'ERROR', { error: error.message }) }
}

async function cancelPending() {
  if (!pendingTaskId.value || busy.value) return
  const sourceConversationId = conversationId.value
  const taskId = pendingTaskId.value
  try {
    await cancelTask(sourceConversationId, taskId)
    await loadConversation(sourceConversationId, false)
    await refreshConversations()
  } catch (error) { receive(sourceConversationId, 'ERROR', { error: error.message }) }
}

async function showTask(taskId) {
  const activeConversationId = conversationId.value
  const detail = await getConversation(activeConversationId)
  if (activeConversationId !== conversationId.value || conversationLoading.value) return
  const task = detail.tasks.find(item => item.id === taskId)
  if (task) applyPersistedTrace(task, detail)
}

async function initialize() {
  try {
    await refreshConversations()
    const pathId = location.pathname.match(/^\/c\/([^/]+)$/)?.[1]
    const target = pathId && conversations.value.some(item => item.id === pathId) ? pathId : conversations.value[0]?.id
    if (target) await loadConversation(target, !pathId)
    else await newTask()
  } catch (error) {
    readiness.value = { status: 'unavailable', ready: false, components: {} }
    conversations.value = []
    messages.value = []
    events.value = []
    trace.value = emptyTrace()
  }
}

function onPopState() {
  const id = location.pathname.match(/^\/c\/([^/]+)$/)?.[1]
  if (id) loadConversation(id, false)
}

onMounted(() => {
  window.addEventListener('popstate', onPopState); initialize(); refreshReadiness()
  sidebarTimer = window.setInterval(() => refreshConversations().catch(() => {}), 3000)
  readinessTimer = window.setInterval(() => refreshReadiness(), 10000)
})
onUnmounted(() => {
  window.removeEventListener('popstate', onPopState); conversationLoader.cancel()
  window.clearInterval(sidebarTimer)
  window.clearInterval(readinessTimer)
  for (const controller of reconnectControllers.values()) controller.abort()
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">S</div><div><strong>Scientific Agent</strong><span>科研任务智能分析</span></div></div>
      <button class="new-task" @click="newTask"><span>＋</span> 新建科研任务</button>
      <div class="side-section conversation-list">
        <template v-for="(items, group) in groupedConversations" :key="group">
          <label v-if="items.length">{{ group }}</label>
          <div v-for="item in items" :key="item.id" :class="['history', { active: item.id === conversationId }]" @click="loadConversation(item.id)">
            <span :class="['history-icon', `status-${sidebarStatus(item)}`]">{{ sidebarStatus(item) === 'completed' ? '✓' : sidebarStatus(item) === 'failed' ? '!' : '◷' }}</span><div class="history-copy"><b>{{ item.title }}</b><small>{{ statusLabels[sidebarStatus(item)] }} · {{ item.message_count }} 条消息</small></div>
            <button title="重命名" @click.stop="renameItem(item)">✎</button><button title="删除" @click.stop="removeItem(item)">×</button>
          </div>
        </template>
      </div>
      <div :class="['side-note', { 'not-ready': !readiness.ready }]"><span class="pulse-dot"></span><div><b>{{ readiness.ready ? '系统已就绪' : readiness.status === 'checking' ? '正在检查服务' : '部分服务未就绪' }}</b><small>{{ readiness.ready ? 'Scientific Agent Ready' : '查看 /ready 或运行 status-dev.ps1' }}</small></div></div>
    </aside>

    <main>
      <header><div><h1>科研任务智能分析 Agent</h1><p>统一理解目标，自动调度文件、数据库与科学工具</p></div><div class="resource-summary"><span class="status-dot"></span><b>{{ resourceCount }}</b> 项可用资源</div></header>
      <section class="resource-bar">
        <div><span class="resource-icon blue">▣</span><p><b>Files</b><small>{{ resources.available_files.join(' · ') || '暂未上传' }}</small></p></div>
        <div><span class="resource-icon green">●</span><p><b>Database</b><small>{{ resources.authorized_datasources.join(' · ') || '无授权数据源' }}</small></p></div>
        <div><span class="resource-icon amber">●</span><p><b>Scientific Models</b><small>{{ resources.available_scientific_models.join(' · ') || '未配置真实权重' }}</small></p></div>
      </section>

      <div class="content-grid">
        <section class="conversation">
          <div v-if="conversationLoading" class="history-loading" aria-label="正在加载历史会话"><i></i><i></i><i></i></div>
          <div v-else-if="!messages.length" class="welcome"><div class="hero-orbit"><span>∿</span></div><h2>从一个科研问题开始</h2><p>无需选择问答模式。描述目标，系统会自动判断所需资源与分析路径。</p><div class="examples"><button v-for="(item, index) in examples" :key="item" @click="query = item"><span>0{{ index + 1 }}</span>{{ item }}</button></div></div>
          <div v-else class="messages">
            <div class="conversation-message-list">
              <article v-for="message in messages" :key="message.id || `${message.role}-${message.created_at}-${message.content}`" :class="['message-row', message.role]">
                <span class="avatar">{{ message.role === 'user' ? '你' : 'S' }}</span><div :class="message.role === 'user' ? 'user-bubble' : 'assistant-body'"><div v-if="message.role === 'assistant'" class="markdown" v-html="marked.parse(message.content)"></div><div v-else>{{ message.content }}</div><button v-if="message.role === 'assistant' && message.task_id" class="show-trace" @click="showTask(message.task_id)">查看执行过程</button></div>
              </article>
            </div>
          </div>
          <div v-if="!conversationLoading && pendingQuestion" class="hitl"><b>需要补充信息</b><p>{{ pendingQuestion }}</p><div><input v-model="answer" placeholder="输入补充信息…" @keyup.enter="resume"><button @click="resume">继续任务</button><button @click="cancelPending">取消任务</button></div></div>
          <div class="composer"><textarea v-model="query" rows="3" placeholder="描述你的科研目标…" @keydown.ctrl.enter.prevent="send"></textarea><div class="composer-actions"><div><input ref="fileInput" type="file" accept=".csv,.xlsx,.xls" hidden @change="handleUpload"><button class="attach" @click="fileInput.click()">↑ 上传 CSV / Excel</button><select v-model="datasourceId"><option value="">不指定数据源</option><option v-for="source in resources.authorized_datasources" :key="source">{{ source }}</option></select><span>Ctrl + Enter 发送</span></div><button class="send" :disabled="currentStatus === 'waiting_for_user' || currentStatus === 'cancelling' || (busy && activeTaskId.startsWith('pending:')) || (!busy && !query.trim() && !(currentStatus === 'failed' && retryableFailedQuery))" @click="primaryAction">{{ currentStatus === 'waiting_for_user' ? '等待补充信息' : currentStatus === 'cancelling' ? '正在取消…' : busy ? '■ 取消执行' : currentStatus === 'failed' ? '重新分析' : '开始分析' }} <span v-if="!busy && currentStatus !== 'waiting_for_user'">→</span></button></div></div>
        </section>

        <aside class="progress-panel">
          <div class="panel-title"><div><span class="live-dot"></span><b>执行轨迹</b></div><small>{{ busy ? 'LIVE' : 'READY' }}</small></div>
          <section class="trace-card"><button class="trace-toggle" @click="traceOpen = !traceOpen"><span>Agent Trace</span><i>{{ traceOpen ? '−' : '+' }}</i></button><div v-if="traceOpen" class="trace-body">
            <div v-if="conversationLoading" class="trace-loading"><i></i><i></i><i></i></div>
            <template v-else>
            <div v-if="trace.interaction" class="trace-block"><label>Interaction</label><p><b>{{ trace.interaction.interaction_type }}</b></p><small>{{ trace.interaction.reason || trace.interaction.source || 'no tool execution' }}</small></div>
            <div v-if="trace.followup" class="trace-block"><label>Follow-up</label><p><b>{{ trace.followup.follow_up_type }}</b></p><small>previous task: {{ trace.followup.previous_task_id || 'none' }} · loaded evidence: {{ trace.followup.loaded_evidence_count }} · new tool calls: {{ trace.followup.new_tool_calls ?? 'pending' }}</small></div>
            <div v-if="trace.intent" class="trace-block"><label>Intent</label><p><b>{{ trace.intent.task_type }}</b> · {{ trace.intent.complexity }}</p><small>{{ trace.intent.domain }} · {{ trace.intent.required_capabilities?.join(' / ') }}</small></div>
            <div v-if="trace.skills.length" class="trace-block"><label>Selected Skills</label><span v-for="skill in trace.skills" :key="skill" class="trace-chip">{{ skill }}</span></div>
            <div v-if="trace.candidates.length" class="trace-block"><label>Candidate Tools</label><span v-for="tool in trace.candidates" :key="tool" class="trace-chip">{{ tool }}</span></div>
            <div v-if="trace.plan.length" class="trace-block"><button class="fold" @click="folds.plan = !folds.plan">Plan <i>{{ folds.plan ? '−' : '+' }}</i></button><template v-if="folds.plan"><p v-for="step in trace.plan" :key="step.step_id" class="trace-row"><i :class="step.status"></i><span>{{ step.step_id }}<small>{{ step.goal }}</small></span></p></template></div>
            <div v-if="trace.tools.length" class="trace-block"><label>Tool Calls</label><p v-for="(tool, i) in trace.tools" :key="`${tool.tool}-${i}`" class="trace-row"><i :class="tool.status"></i><span>{{ tool.tool }}</span><small>{{ tool.status }}</small></p></div>
            <div v-if="trace.evidence.length" class="trace-block"><button class="fold" @click="folds.evidence = !folds.evidence">Evidence <i>{{ folds.evidence ? '−' : '+' }}</i></button><template v-if="folds.evidence"><p v-for="item in trace.evidence" :key="item.id || item.evidence_id" class="trace-evidence"><b>{{ item.claim }}</b><small>{{ item.source }}</small></p></template></div>
            <div v-if="trace.claims.length" class="trace-block"><label>Claims → Evidence</label><p v-for="(claim, i) in trace.claims" :key="claim.id || i" class="trace-evidence"><b>{{ claim.claim_text || claim.text }}</b><small>{{ claim.status }} · {{ (claim.evidence_ids_json || claim.evidence_ids || []).join(', ') || 'unsupported' }}</small></p></div>
            <div v-if="trace.sql" class="trace-block"><label>SQL Source</label><pre class="trace-evidence">{{ trace.sql.sql }}</pre><small>Params: {{ JSON.stringify(trace.sql.params || {}) }}</small></div>
            <div v-if="trace.artifacts.length" class="trace-block"><button class="fold" @click="folds.artifacts = !folds.artifacts">Artifacts <i>{{ folds.artifacts ? '−' : '+' }}</i></button><template v-if="folds.artifacts"><button v-for="item in trace.artifacts" :key="item.artifact_id" class="artifact" @click="downloadArtifact(item.artifact_id, item.filename)">↓ {{ item.filename }}</button></template></div>
            <div v-if="trace.waiting" class="trace-wait"><b>HITL</b><p>{{ trace.waiting }}</p></div>
            </template>
          </div></section>
          <div ref="timeline" class="timeline"><div v-if="conversationLoading" class="timeline-loading"><i></i><i></i><i></i></div><div v-else-if="!events.length" class="empty-progress"><span>◷</span><p>任务开始后，这里会实时展示意图、计划、工具、证据与产物。</p></div><template v-else><div v-for="(item, index) in events" :key="index" :class="['event', item.type.toLowerCase()]"><i>{{ item.type === 'ERROR' ? '!' : item.type === 'FINAL_ANSWER' ? '✓' : index + 1 }}</i><div><b>{{ item.text }}</b><p>{{ item.detail }}</p><small>{{ item.time }}</small></div></div></template></div>
        </aside>
      </div>
    </main>
  </div>
</template>
