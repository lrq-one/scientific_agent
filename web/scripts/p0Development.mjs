// Reuse only the real UI mechanics, never a frozen execution destination.
import { readFile } from 'node:fs/promises'
const source = await readFile(new URL('./frozenBenchmark.mjs', import.meta.url), 'utf8')
const selected = process.argv.find(x=>x.startsWith('--cases='))?.split('=')[1]?.split(',')
const phase = process.argv.find(x=>x.startsWith('--phase='))?.split('=')[1] || 'smoke'
if (!/^[a-z0-9_-]+$/i.test(phase)) throw new Error('Invalid development phase')
const ids = selected || ['D01','D02','D06','D09','M01','M02','M07','M08','H01','H02','H03','H06','F01','F05','M04','M05','U01','U05','D08','F10']
// The provider receives only original queries/files. Development outcomes are
// evaluated separately; Gold never appears in an Agent request.
const replacement = `const all=(await readFile(path.join(report,'benchmark_manifest.jsonl'),'utf8')).trim().split('\\n').map(JSON.parse).filter(c=>${JSON.stringify(ids)}.includes(c.case_id)); for(const c of all){c.development_source_case_id=c.case_id; c.case_id=${JSON.stringify(phase)}+'-'+c.case_id; c.state_action=null}`
let code=source.replace("const folder=path.join(report,smoke?developmentName:`runs/${variant}`)",
  `const folder=path.join(root,'reports/phase4a_p0_hardening_20261008/ui/${phase}')`)
const start=code.indexOf('const all=smoke'), end=code.indexOf('\nconst subset=',start)
if(start<0||end<0)throw new Error('UI harness structure changed')
code=code.slice(0,start)+replacement+code.slice(end)
code=code.replace("const concurrency=smoke?1:4",'const concurrency=2')
code=code.replace('await page.goto(frontend);', "await page.goto(frontend,{waitUntil:'domcontentloaded',timeout:60000});")
code=code.replace("const variant=process.argv[2] || 'full'",`const variant='P0_DEVELOPMENT_NOT_FROZEN'`)
code=code.replace("entrypoint:'real_vue'", "entrypoint:'real_vue',development_source_case_id:c.development_source_case_id,label:'NOT FROZEN TEST / NOT UNSEEN EVALUATION'")
// Keep relative UI runner location without editing or overwriting frozen code.
code=code.replace("const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..')",`const root=${JSON.stringify(new URL('../..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/,'$1').replace(/\/$/,''))}`)
// Resolve paths through fileURLToPath, preserving non-ASCII Windows roots.
code=code.replace(/const root=.*\n/,`const root=${JSON.stringify((await import('node:url')).fileURLToPath(new URL('../..',import.meta.url)))}\n`)
const cwd=process.cwd()
const {writeFile}=await import('node:fs/promises')
const generated=new URL(`./.p0-${phase}.mjs`,import.meta.url)
await writeFile(generated,code,{flag:'wx'})
await import(generated.href)
