// REAL Vue + FastAPI/SSE. No route interception, mocked responses or gold input.
import { chromium } from 'playwright'
import { mkdir, readFile, writeFile, access } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root="D:\\工作\\scientific_agent\\"
const report=path.join(root,'reports/phase4_v2_20261008')
const variant='P0_DEVELOPMENT_NOT_FROZEN'
const smoke=process.argv.includes('--dev-smoke')
const endpoint='http://127.0.0.1:8000'.replace('8000','8002')
const frontend='http://127.0.0.1:5175'
const headers={'X-User-Id':'demo-researcher'}
const developmentName=process.argv.find(arg=>arg.startsWith('--dev-dir='))?.split('=')[1] || 'development'
const folder=path.join(root,'reports/phase4a_p0_hardening_20261008/ui/repeat3')
await mkdir(folder,{recursive:true})
const all=(await readFile(path.join(report,'benchmark_manifest.jsonl'),'utf8')).trim().split('\n').map(JSON.parse).filter(c=>["D01","D02","D06","D09","M01","M02","H01","H02","U05","D08"].includes(c.case_id)); for(const c of all){c.development_source_case_id=c.case_id; c.case_id="repeat3"+'-'+c.case_id; c.state_action=null}
const subset={no_skill:'skill',no_replan:'replan',stateless_followup:'followup',no_evidence_gate:'evidence'}[variant]
const cases=all.filter(c=>!subset || c.subsets.includes(subset))
const concurrency=2
if (!smoke) await access(path.join(report,'freeze_manifest.json'))
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
let cursor=0, infraFailure=null
async function run(c) {
  const destination=path.join(folder,`${c.case_id}.json`)
  try {await access(destination);throw new Error(`Existing record must not be overwritten: ${c.case_id}`)} catch(e) {if(e.code!=='ENOENT')throw e}
  const ctx=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true})
  const page=await ctx.newPage(), http=[],sse=[],errors=[],pendingBodies=[]
  const record={case_id:c.case_id,variant,tags:c.tags,started_at:new Date().toISOString(),entrypoint:'real_vue',development_source_case_id:c.development_source_case_id,label:'NOT FROZEN TEST / NOT UNSEEN EVALUATION',turns:[],http,sse,page_errors:errors}
  page.on('pageerror',e=>errors.push(e.message))
  page.on('response',r=>{
    if(!r.url().startsWith(endpoint))return
    const request=r.request()
    http.push({endpoint:r.url().slice(endpoint.length),method:request.method(),status:r.status(),timing:request.timing()})
    if(r.headers()['content-type']?.includes('text/event-stream')) pendingBodies.push(r.text().then(body=>sse.push({endpoint:r.url().slice(endpoint.length),body})).catch(e=>sse.push({error:e.message})))
  })
  async function detail() {
    const r=await page.request.get(`${endpoint}/api/conversations/${record.conversation_id}`,{headers})
    if(!r.ok()) throw new Error(`Inspection HTTP ${r.status()}`)
    return r.json()
  }
  async function settle() {
    await page.locator('.composer textarea').waitFor({timeout:30000})
    await page.locator('.history.active').waitFor({timeout:30000})
    await page.locator('.history-loading').waitFor({state:'hidden',timeout:30000})
    await page.waitForTimeout(350)
  }
  async function waitTerminal(before,taskId=null,ignoreWaitingThrough=0) {
    const started=Date.now()
    while(Date.now()-started<280000) {
      const d=await detail(),task=taskId?d.tasks.find(t=>t.id===taskId):d.tasks.find(t=>!before.has(t.id))
      const freshWait=task && d.events.some(e=>e.task_id===task.id && e.event_type==='WAITING_FOR_USER' && e.id>ignoreWaitingThrough)
      if(task && (['completed','failed','cancelled'].includes(task.status) || task.status==='waiting_for_user' && freshWait))return {d,task}
      await page.waitForTimeout(600)
    }
    const d=await detail(),task=taskId?d.tasks.find(t=>t.id===taskId):d.tasks.find(t=>!before.has(t.id))
    return {d,task,driver_timeout:true}
  }
  try {
    for(let attempt=0;attempt<45;attempt++) {
      const health=await page.request.get(`${endpoint}/health`).catch(()=>null)
      if(health?.ok())break
      if(attempt===44)throw new Error('Evaluation backend did not become healthy')
      await page.waitForTimeout(1000)
    }
    await page.goto(frontend,{waitUntil:'domcontentloaded',timeout:60000});await page.locator('.new-task').waitFor()
    await page.waitForTimeout(1000)
    const created=page.waitForResponse(r=>r.request().method()==='POST' && r.url()===`${endpoint}/api/conversations`)
    await page.locator('.new-task').click();record.conversation_id=(await (await created).json()).id
    await page.waitForURL(`${frontend}/c/${record.conversation_id}`);await settle()
    for(const name of c.files) {
      const upload=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().includes('/files/upload'))
      await page.locator('input[type="file"]').setInputFiles(path.join(report,'fixtures',name))
      const response=await upload
      if(!response.ok())throw new Error(`UI upload infrastructure failed: ${response.status()}`)
      await page.waitForTimeout(350)
    }
    for(const turn of c.user_turns) {
      const initial=await detail(),before=new Set(initial.tasks.map(t=>t.id)),start=Date.now()
      const t={query:turn.query,started_at:new Date().toISOString(),human_wait_ms:0}
      await page.locator('.composer textarea').fill(turn.query)
      await page.locator('.composer .send').click()
      let terminal=await waitTerminal(before)
      if(!terminal.task) {
        t.submission_failure=true;t.detail=terminal.d;t.latency_ms=Date.now()-start;record.turns.push(t)
        break
      }
      const waits=[]
      let rounds=0
      while(terminal.task.status==='waiting_for_user' && turn.hitl_answer && rounds++<2) {
        const waitingAt=Date.now(),posts=http.filter(h=>h.method==='POST').length
        const wait={task:terminal.task,events:terminal.d.events.filter(e=>e.task_id===terminal.task.id)}
        await page.reload();await settle()
        wait.refresh_extra_posts=http.filter(h=>h.method==='POST').length-posts
        wait.frontend_question=await page.locator('.hitl').innerText()
        await page.locator('.hitl input').fill(turn.hitl_answer)
        await page.locator('.hitl button').filter({hasText:'继续任务'}).click()
        wait.answer=turn.hitl_answer;wait.human_wait_ms=Date.now()-waitingAt;t.human_wait_ms+=wait.human_wait_ms
        const old=terminal.task
        terminal=await waitTerminal(before,old.id,Math.max(0,...wait.events.map(e=>e.id)))
        wait.same_context=terminal.task.id===old.id && terminal.task.thread_id===old.thread_id && terminal.task.conversation_id===old.conversation_id
        waits.push(wait)
      }
      if(terminal.task.status==='waiting_for_user') {
        t.unanswered_hitl=true
        // No invented clarification answer; cancel through the real UI and preserve ASK.
        await page.locator('.hitl button').filter({hasText:'取消任务'}).click()
        terminal=await waitTerminal(before,terminal.task.id)
      }
      if(terminal.driver_timeout) {
        t.driver_timeout=true
        await page.locator('.composer .send').click()
        await page.waitForTimeout(1500);terminal.d=await detail();terminal.task=terminal.d.tasks.find(x=>x.id===terminal.task.id)
      }
      await page.waitForTimeout(600)
      const d=await detail(),task=d.tasks.find(x=>x.id===terminal.task.id)
      t.task=task;t.waits=waits;t.events=d.events.filter(e=>e.task_id===task.id)
      for(const k of ['evidence','claims','artifacts','messages'])t[k]=d[k].filter(x=>x.task_id===task.id)
      t.frontend_answer=await page.locator(`.message-row.assistant[data-task-id="${task.id}"]`).allInnerTexts()
      t.frontend_errors=await page.locator(`.message-row.error[data-task-id="${task.id}"]`).allInnerTexts()
      t.frontend_trace=await page.locator('.trace-block').allInnerTexts()
      t.latency_ms=Date.now()-start;t.system_latency_ms=t.latency_ms-t.human_wait_ms
      if(t.artifacts.length) {
        const buttons=page.locator('.artifact')
        t.downloads=[]
        for(let a=0;a<await buttons.count();a++) {
          const promise=page.waitForEvent('download',{timeout:15000});await buttons.nth(a).click()
          const download=await promise,filename=`${c.case_id}-${task.id}-${a}-${download.suggestedFilename()}`
          const failure=await download.failure();if(!failure)await download.saveAs(path.join(folder,filename))
          t.downloads.push({filename,failure})
        }
      }
      const posts=http.filter(h=>h.method==='POST').length
      await page.reload();await settle()
      t.reload_extra_posts=http.filter(h=>h.method==='POST').length-posts
      t.reload_answer=await page.locator(`.message-row.assistant[data-task-id="${task.id}"]`).allInnerTexts()
      t.reload_button_text=await page.locator('.composer .send').innerText()
      await page.screenshot({path:path.join(folder,`${c.case_id}-turn${record.turns.length+1}.png`),fullPage:true})
      record.turns.push(t)
      await writeFile(destination,JSON.stringify(record,null,2),{flag:'w'})
      console.log(JSON.stringify({variant,case_id:c.case_id,turn:record.turns.length,status:task.status,latency_ms:t.latency_ms,tools:t.events.filter(e=>e.event_type==='TOOL_FINISHED').length}))
      if(task.status==='cancelled' || t.driver_timeout) break
    }
    // Cross-user boundary is tested with a read-only request to the same persisted ID.
    const denied=await page.request.get(`${endpoint}/api/conversations/${record.conversation_id}`,{headers:{'X-User-Id':'evaluation-other-user'}})
    record.cross_user_http_status=denied.status()
    if(c.state_action==='archive') {
      const item=page.locator(`.history[data-conversation-id="${record.conversation_id}"]`)
      await item.locator('button[title="删除"]').click()
      const dialog=page.locator('dialog[open]');record.archive_dialog=await dialog.innerText()
      await dialog.locator('button').filter({hasText:/删除/}).click();await page.waitForTimeout(500)
      const archived=await page.request.get(`${endpoint}/api/conversations/${record.conversation_id}`,{headers})
      record.archived_http_status=archived.status()
    }
    if(c.state_action==='isolation') {
      const old=record.conversation_id,posts=http.filter(h=>h.method==='POST').length
      const create=page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===`${endpoint}/api/conversations`)
      await page.locator('.new-task').click();const fresh=(await (await create).json()).id;await settle()
      record.isolation={old_conversation:old,new_conversation:fresh,new_assistant_count:await page.locator('.message-row.assistant').count(),new_trace_count:await page.locator('.trace-evidence').count()}
      await page.locator(`.history[data-conversation-id="${old}"]`).click();await settle()
      record.isolation.returned_to_original=page.url().endsWith(old)
      record.isolation.execution_posts=http.filter(h=>h.method==='POST').length-posts-1
    }
  } catch(error) {
    record.infrastructure_error=error.message
    infraFailure=error
    await page.screenshot({path:path.join(folder,`${c.case_id}-infrastructure-failure.png`),fullPage:true}).catch(()=>{})
  } finally {
    record.finished_at=new Date().toISOString()
    await Promise.allSettled(pendingBodies)
    await writeFile(destination,JSON.stringify(record,null,2))
    await ctx.close()
  }
}
try {
  await Promise.all(Array.from({length:concurrency},async()=>{
    while(cursor<cases.length && !infraFailure) {const c=cases[cursor++];await run(c)}
  }))
} finally {await browser.close()}
if(infraFailure)throw infraFailure
console.log(JSON.stringify({variant,finished:cases.length,concurrency,smoke}))
