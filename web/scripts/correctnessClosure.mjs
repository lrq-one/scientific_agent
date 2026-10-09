// One sequential real Vue run per existing development case; no reruns.
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../..', import.meta.url))
const ids = ['D09', 'D08', 'D06', 'M02', 'U05', 'H01']
let source = await readFile(new URL('./frozenBenchmark.mjs', import.meta.url), 'utf8')
source = source.replace("const variant=process.argv[2] || 'full'", "const variant='CORRECTNESS_CLOSURE_DEVELOPMENT'")
source = source.replace("const folder=path.join(report,smoke?developmentName:`runs/${variant}`)",
    "const folder=path.join(root,'reports/phase4a_correctness_closure_20261009/ui')")
const start = source.indexOf('const all=smoke'), end = source.indexOf('\nconst subset=', start)
if (start < 0 || end < 0) throw new Error('Original UI harness changed')
source = source.slice(0, start) + `const ids=${JSON.stringify(ids)}
const manifest=(await readFile(path.join(report,'benchmark_manifest.jsonl'),'utf8')).trim().split('\\n').map(JSON.parse)
const all=ids.map(id=>{const c=manifest.find(item=>item.case_id===id);if(!c)throw new Error('Missing development case '+id);return {...c,development_source_case_id:id,case_id:'closure-'+id,state_action:null}})` + source.slice(end)
source = source.replace('const concurrency=smoke?1:4', 'const concurrency=1')
source = source.replace('await page.goto(frontend);', "await page.goto(frontend,{waitUntil:'domcontentloaded',timeout:60000});")
source = source.replace("entrypoint:'real_vue'", "entrypoint:'real_vue',development_source_case_id:c.development_source_case_id")
source = source.replace("{const c=cases[cursor++];await run(c)}", `{const c=cases[cursor++];await run(c);
if(!infraFailure){const {spawnSync}=await import('node:child_process');const audit=spawnSync(path.join(root,'.venv/Scripts/python.exe'),['-m','evaluation.closure_smoke_summary','--check-record',path.join(folder,c.case_id+'.json')],{cwd:root,encoding:'utf8'});if(audit.status!==0){console.error(audit.stdout||audit.stderr);infraFailure=new Error('Systemic correctness failure; remaining smoke stopped')}}}`)
const generated = new URL('./.correctness-closure-run.mjs', import.meta.url)
await writeFile(generated, source, { flag: 'wx' })
await import(generated.href)
