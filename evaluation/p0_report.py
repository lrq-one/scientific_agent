"""Development-only metric builds from immutable UI records and provider telemetry."""
from collections import defaultdict
from pathlib import Path
import csv
import hashlib
import json
import statistics
import subprocess
from evaluation.p0_audit import OUT, ROOT, load, audit, integrity

LABEL = 'NOT FROZEN TEST / NOT UNSEEN EVALUATION'
REPEATED = ['D01','D02','D06','D09','M01','M02','H01','H02','U05','D08']
GROUPS = {'DB':['D01','D02','D06','D09'], 'Mixed':['M01','M02','M07','M08'],
          'Version/HITL':['H01','H02','H03','H06'], 'Replan':['F01','F05','M04','M05'],
          'Follow-up':['U01','U05'], 'Evidence/Artifact':['D08','F10']}

def rate(n,d): return f'{n}/{d} ({n/d*100:.1f}%)' if d else 'NOT_MEASURED (no eligible observations)'

def write_markdown(results, check):
    dev=[r for r in results if r['run']=='development20v5']
    repeated=[r for r in results if r['run'].startswith('repeat')]
    previous=[]
    for r in dev:
        d=load(ROOT/f'reports/phase4_v2_20261008/grading_v1/runs/full/{r["case_id"]}.json')
        previous.append({'case_id':r['case_id'],'asks':sum(e['event_type']=='WAITING_FOR_USER' for t in d['turns'] for e in t['events']),
            'rejections':sum(e['event_type'] in {'PLAN_REJECTED','DECISION_REJECTED'} for t in d['turns'] for e in t['events']),
            'latency_ms':sum(t.get('system_latency_ms',t.get('latency_ms',0)) for t in d['turns']),
            'unnecessary':not r['clarification_required']})
    group_lines=[]
    for name,ids in GROUPS.items():
        eligible=[r for r in dev if r['case_id'] in ids]
        group_lines.append(f'| {name} | {rate(sum(r["baseline_success"] for r in eligible),len(eligible))} | {rate(sum(r["development_contract_pass"] for r in eligible),len(eligible))} |')
    necessary=[r for r in dev if r['clarification_required']]
    asks=sum(r['asks'] for r in dev)
    true_asks=sum(r['asks'] for r in necessary)
    before_unnecessary=sum(r['asks'] for r in previous if r['unnecessary'])
    after_unnecessary=sum(r['unnecessary_asks'] for r in dev)
    recovered=[r['case_id'] for r in dev if not r['baseline_success'] and r['development_contract_pass']]
    failed=[r for r in dev if not r['development_contract_pass']]
    plans=sum(r['plan_steps'] for r in dev)
    sql=sum(r['successful_sql'] for r in dev)
    replanned=[r for r in dev if r['replans']]
    code=load(OUT/'development_code_version_v5.json')['source_hashes']
    source_changes=[p for p,h in code.items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    complete=len(dev)==20 and len(repeated)==30 and all(x['runs']==3 for x in check['repeated'].values()) and all(r['ui_record_finished'] for r in results)
    repeats='\n'.join(f'| {c} | {x["passes"]}/{x["runs"]} | {json.dumps(x["failure_reasons"],ensure_ascii=False)} |' for c,x in check['repeated'].items())
    text=f'''# Phase 4A.1 P0 Reliability Hardening

**NOT FROZEN TEST / NOT UNSEEN EVALUATION**

Original formal phase4-v2.0 baseline stays **29/60 = 48.3%**, permanently unchanged. This directory is development evidence only.

## Status and denominators

Observed development records: {len(dev)}/20. Observed repeated records: {len(repeated)}/30. Measurement set complete: {complete}. **Reliability acceptance remains open** (see failure_review.md).

Development execution-contract success: {rate(sum(r['development_contract_pass'] for r in dev),len(dev))}. Repeated execution-contract success: {rate(sum(r['development_contract_pass'] for r in repeated),len(repeated))}.

“Pass” requires completed task(s), no failure quality/fallback/UI errors, all expected user turns, actual persisted numeric facts, required Evidence/artifact and verified executed SQL scope. Numeric checks compare persisted values against the unchanged synthetic oracle; matching numbers alone is not independent scientific semantic grading. Original baseline columns use the old frozen judge and are NOT a newly regraded score. These two scoring contracts differ. No unseen generalization claim is made.

The retained v5 ToolResults are not rewritten. The final v9 read-only scope re-audit disproved two primary historical `verified=true` tags (repeat2-D09 and repeat3-D09), plus the v7 diagnostic feature-only total branch. Conservative reported success excludes primary refutations; CSV also retains `historical_contract_pass` separately. See scope_reaudit.json. This re-audit parses retained SQL and reads schema metadata only; it executes no analysis SQL and calls no LLM.

| Development group | Original baseline judge (same case subset) | New execution contract |
| --- | --- | --- |
{chr(10).join(group_lines)}

Old failed cases now passing this development contract: {', '.join(recovered) or 'none observed'}.
Current failed cases: {', '.join(r['case_id'] for r in failed) or 'none observed'}.

## P0 mechanisms

1. Authorized ResourceBinding separates dataset/version/run identities; metadata and previous scope resolve arbitrary labels. Pre-ASK sufficiency sends deterministic control feedback without choosing Agent tools. Real ambiguous files/versions still require clarification.
2. Runtime-owned step conditions/lifecycle, step-bound observations, identical-plan retention, transactional replacement with verified dependency retention, capability/DAG guards and successful recovery counter reset. Structured output completion IDs follow the same conditions.
3. Original Goal + local Goal + QueryScope reach Text-to-SQL. Candidate/check/executed SQL, actual rows and Evidence retain scope. Whole/train queries can be verified local branches, but early FINISH is rejected until actual executed populations jointly cover the requested comparison. Authorized run lineage proves exact version only from trusted metadata. Version-only follow-up retains prior scope.

Detailed changes and file/function references: [code_changes.md](code_changes.md). Targeted unit verification: [targeted_tests.md](targeted_tests.md).

## Direct P0 observations

- Unnecessary ASK in the same observed subset: before {before_unnecessary}, after {after_unnecessary}. Required-clarification cases are identified by explicit unresolved selections, not by post-hoc success.
- HITL Precision (ASK actions on required-clarification cases / all ASK actions): {rate(true_asks,asks)}. HITL Recall (required cases that actually ASK / required cases): {rate(sum(r['asks']>0 for r in necessary),len(necessary))}. Multiple ASK rounds are separate actions; no assertion of perfect clarification is inferred from success.
- Plan/decision rejection events: before {sum(r['rejections'] for r in previous)}, after {sum(r['plan_rejections']+r['decision_rejections'] for r in dev)}. Rejection is an enforced guard, not by itself a guard defect. New invalid plan proposals: {sum(r['plan_rejections'] for r in dev)}. Plan steps completed: {rate(sum(r['completed_steps'] for r in dev),plans)}.
  Step completion/replan denominators require a persisted FINAL_ANSWER state; outer ERROR paths without that state are unmeasured, not zero-step successes. Event-level rejection counts still include these paths.
- Replan recovery among observed cases with an actual replacement: {rate(sum(r['development_contract_pass'] for r in replanned),len(replanned))}. No old counter semantics are silently equated with the new protocol.
  Baseline recovery under this same accounting = NOT_MEASURED; the original protocol lacks the same replacement/completion conditions. Lower rejection counts do not prove successful recovery.
- Actual successful SQL executions: {sql}. Scope verification tags: {sum(s['scope_verified'] for r in dev for s in r['scope_rows'])}/{sql}. This does not claim that every rejected candidate was correct, or that every scientific goal is fully satisfied.
- Measured provider tokens: {sum(r['tokens'] for r in dev):,}; measured calls: {sum(r['provider_calls'] for r in dev)}; provider records without token measurement: {sum(r['unmeasured_provider_calls'] for r in dev)}. Only observed usage is summed. All successful provider calls use the recorded actual model, not inferred configuration.
- Actual provider models: {sorted({m for r in results for m in r['actual_models']})}. Provider HTTP failures across designated trials: {sum(r['provider_http_errors'] for r in results)}. Conversations with an explicitly reported fallback: {sum(any('fallback' in t['reasons'] for t in r['turns']) for r in results)}. Outer ERROR paths without final telemetry cannot prove fallback=false.
- Median conversation system latency: {statistics.median([r['total_system_latency_ms'] for r in dev]) if dev else 'NOT_MEASURED'} ms. Development browser concurrency is 2, original full baseline concurrency was 4. Latency is not a controlled performance improvement estimate; performance optimization is P2.

## Repeated-run stability

Each repeat uses a fresh conversation, identical original inputs and identical scientific snapshot. Product source changes since development version capture: {json.dumps(source_changes)}.

All 20+30 UI runs used captured v5 Product/harness. The source changes listed above are **subsequent independent-branch validation and schema-prerequisite projection**, not mid-experiment edits. Source captures and local UI records are separate; final v9 Product has NOT been tested on a replacement 20+30 set or a new full UI run. The final guard recognizes scientific entity populations from authorized schema columns even without a split column or a populated optional entity hint. Do not attribute v5 results to a fully retested latest release. See [post_measurement_checks.md](post_measurement_checks.md).

`repeat2_remaining` only fills cases never started after a transport interruption; its records belong to logical run `repeat2`. The original repeat2-M01 ECONNRESET remains a failed E2E observation, not replaced by a successful retry. Infrastructure failures are included conservatively in the denominator. No failed scientific result is rerun selectively.

| Source task | Passes / observed runs | Failed-run reasons |
| --- | --- | --- |
{repeats}

**Before repeated-run stability = NOT_MEASURED.** The old baseline did not perform three same-state fresh trials. Therefore this report does not claim a quantified stability improvement against an unavailable denominator. New instability/failure reasons remain visible.

## Integrity and protected boundaries

Original report files changed: {json.dumps(check['original_report_changed_files'])}. Original scientific snapshot unchanged: {check['original_scientific_snapshot_unchanged']}. Development scientific snapshot unchanged: {check['development_scientific_snapshot_unchanged']}.

Protected file hashes: `{json.dumps(check['protected_source_files_unchanged'],ensure_ascii=False)}`. Security, SQLGuard, Skill catalog/router, Grounded Response, checkpointing and object storage are not rewritten. Evidence receives scope lineage only; Dispatcher receives context/validation adapters only.

No API key is included in this report. Database clone is `phase4a_p0_development_20261008`. User services 8000/5173 were not stopped or reconfigured; development uses 8002/5175.

## Report data and remaining work

Required flat CSV outputs: development_regression.csv, repeated_runs.csv, version_grounding_results.csv, plan_protocol_results.csv, sql_scope_results.csv. Each row links to a retained real UI execution, task/thread/conversation identity or actual SQL/result.

See [remaining_failures.md](remaining_failures.md) for retained failures and P1 scope, and [failure_review.md](failure_review.md) for actual event evidence and source references. [git_diff_summary.md](git_diff_summary.md) contains the complete git status and distinguishes P0 source changes from pre-existing dirty changes. Pilot/first-batch failures are preserved and excluded from the final version's primary denominators, never deleted or overwritten.

P0 implementation and measurement completion do not imply reliability acceptance. Remaining invalid plans, failed recovery and scope/goal gaps keep the acceptance open. Do not promote this development version as a reliably completed P0 release.
'''
    (OUT/'README.md').write_text(text,encoding='utf-8')
    failure_text='# Remaining failures\n\n'+LABEL+'\n\n'
    for r in failed:
        failure_text+=f'## {r["case_id"]}\n\nReasons: {r["reasons"]}. Real record: `{r["source_record"]}`.\n\n'
        for t in r['turns']:
            failure_text+=f'Turn {t["turn"]}, task `{t["task_id"]}`, thread `{t["thread_id"]}`. SQL successes={t["successful_sql_count"]}, Evidence={t["evidence_count"]}. Errors: {json.dumps(t["final_errors"],ensure_ascii=False)}\n\n'
    failure_text+='\nP1 next: Tool contract/Final Goal Coverage and Grounded Response failure semantics; Evidence content/scope independent verification and follow-up State Sufficiency; evaluation contract alignment and safe-refusal terminal semantics. P2 taxonomy/context/performance work is not implemented here.\n\nThe first development20 batch was interrupted by a page-load timeout at M04. Its earlier D09 local-branch scope defect triggered a general P0 repair. pilot5 had an async-operation timeout. A later D08 executed SQL, saved Evidence and downloaded CSV but failed Grounded Response validation. These attempts remain under ui/ and are not counted as successes.\n'
    (OUT/'remaining_failures.md').write_text(failure_text,encoding='utf-8')
    git=Path('C:/Users/LWH/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe')
    status=subprocess.check_output([str(git),'status','--short'],cwd=ROOT,text=True,encoding='utf-8',stderr=subprocess.PIPE)
    diff=subprocess.check_output([str(git),'diff','--stat'],cwd=ROOT,text=True,encoding='utf-8',stderr=subprocess.PIPE)
    frozen=load(ROOT/'reports/phase4_v2_20261008/freeze_manifest.json')
    changed=[p for p,h in frozen['source_hashes'].items() if (ROOT/p).is_file() and hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    (OUT/'git_diff_summary.md').write_text('# Git scope\n\nExisting dirty changes are preserved. Git diff is against HEAD and includes prior user work, not a P0-only diff. No commit or push performed.\n\nProduct files changed against the frozen source hashes:\n\n'+ '\n'.join('- '+p for p in changed)+'\n\nNew P0 files are documented in code_changes.md.\n\n## git status --short\n\n```text\n'+status+'```\n\n## git diff --stat (includes pre-existing changes)\n\n```text\n'+diff+'```\n',encoding='utf-8')
    import ast
    references={}
    for p in changed+['app/agents/plan_protocol.py','app/services/query_scope.py','app/services/resource_grounding.py']:
        tree=ast.parse((ROOT/p).read_text(encoding='utf-8-sig'))
        references[p]=[{'function':n.name,'line':n.lineno} for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
    (OUT/'code_references.json').write_text(json.dumps(references,indent=2),encoding='utf-8')

def numeric_values(value):
    if isinstance(value, bool): return []
    if isinstance(value, (int,float)): return [value]
    if isinstance(value, dict): return [v for x in value.values() for v in numeric_values(x)]
    if isinstance(value, list): return [v for x in value for v in numeric_values(x)]
    return []

def summarize(path, attempts):
    record = load(path)
    case = record.get('development_source_case_id') or record['case_id']
    judged = load(ROOT/f'reports/phase4_v2_20261008/grading_v1/judgements/full/{case}.json')
    gold = judged['input']['gold']
    result = audit(record)
    reasons = [r for t in result['turns'] for r in t['reasons']]
    if record.get('finished_at') and record.get('cross_user_http_status') not in {403,404}:
        reasons.append('cross_user_boundary_failed_or_unmeasured')
    expected_queries=[t['query'] for t in judged['input']['case']['user_turns']]
    if any(t['query']!=expected_queries[i] for i,t in enumerate(record['turns']) if i<len(expected_queries)):
        reasons.append('original_user_input_changed')
    if 'database' in gold.get('required_capabilities',[]) and not any(t['successful_sql_count'] for t in result['turns']):
        reasons.append('required_database_analysis_has_no_executed_sql')
    if len(record['turns']) != len(judged['input']['case']['user_turns']): reasons.append('missing_user_turn')
    fact_turn = gold.get('numeric_facts_apply_to_turn', 0)
    factual = record['turns'][fact_turn] if fact_turn < len(record['turns']) else {}
    actual_numbers = numeric_values([e.get('value_json') for e in factual.get('evidence', [])])
    facts = gold.get('expected_numeric_facts', [])
    missing = [f for f in facts if not any(abs(v-f['value'])<=gold.get('acceptable_result_tolerance',.001) for v in actual_numbers)]
    if missing: reasons.append('required_numeric_fact_not_in_persisted_evidence')
    if gold.get('required_evidence') and not any(t.get('evidence') for t in record['turns']): reasons.append('missing_evidence')
    if gold.get('artifact_required') and not any(t.get('artifacts') for t in record['turns']): reasons.append('missing_required_artifact')
    for t in record['turns']:
        if any(d.get('failure') for d in t.get('downloads', [])): reasons.append('artifact_download_failure')
    if record.get('infrastructure_error'): reasons.append('UI_infrastructure_error')
    provider = [r for r in attempts if r.get('conversation_id')==record.get('conversation_id') and r.get('phase')=='provider_finished']
    scope_rows=[]
    for turn in result['turns']:
        for executed in turn['sql']:
            metadata=executed['result']['metadata']
            scope_rows.append({'case_id':case, 'run':path.parent.name, 'turn':turn['turn'],
                'conversation_id':record['conversation_id'],'task_id':turn['task_id'],
                'thread_id':turn['thread_id'],'dataset_version':turn['scope'].get('dataset_version'),
                'tool_call_id':executed.get('tool_call_id'),
                'split':turn['scope'].get('split'), 'whole_dataset':turn['scope'].get('whole_dataset'),
                'sql':metadata['sql'],'params':json.dumps(metadata['params'],ensure_ascii=False),
                'rows':json.dumps(executed['result']['data'],ensure_ascii=False),
                'scope_verified':metadata.get('scope_validation',{}).get('verified',False),
                'evidence_count':turn['evidence_count'],'source_record':str(path.relative_to(OUT))})
    return {'case_id':case,'run':path.parent.name, 'conversation_id':record.get('conversation_id'), 'ui_record_finished':bool(record.get('finished_at')),
        'baseline_success':judged['output']['overall_success'],
        'development_contract_pass':not reasons, 'reasons':sorted(set(reasons)),
        'asks':sum(t['ask_count'] for t in result['turns']),
        'unnecessary_asks':sum(t['ask_count'] for t in result['turns']) if not gold.get('clarification_required') else 0,
        'clarification_required':gold.get('clarification_required',False),
        'plan_rejections':sum(t['plan_rejections'] for t in result['turns']),
        'decision_rejections':sum(t['decision_rejections'] for t in result['turns']),
        'plan_steps':sum(t['plan_steps'] for t in result['turns']),
        'completed_steps':sum(t['completed_steps'] for t in result['turns']),
        'replans':sum(t['replan_count'] for t in result['turns']),
        'tool_calls':sum(t['tool_calls'] for t in result['turns']),
        'successful_sql':len(scope_rows), 'tokens':sum((r.get('usage') or {}).get('total_tokens',0) for r in provider),
        'unmeasured_provider_calls':sum(not isinstance((r.get('usage') or {}).get('total_tokens'),int) for r in provider),
        'provider_calls':len(provider),'provider_http_errors':sum(r.get('http_status')!=200 for r in provider),
        'actual_models':sorted({r.get('actual_model') for r in provider if r.get('actual_model')}),
        'total_system_latency_ms':sum(t.get('latency_ms') or 0 for t in result['turns']),
        'turns':result['turns'],'scope_rows':scope_rows,'missing_facts':missing,
        'source_record':str(path.relative_to(OUT)), 'label':LABEL}

def normalize_trials(results):
    """Keep source identity; missing-case supplements cannot replace outcomes."""
    for r in results:
        r['source_phase']=r['run']
        if r['run']=='repeat2_remaining':
            r['run']='repeat2'
            for s in r['scope_rows']: s['run']='repeat2'
    identities=[(r['run'],r['case_id']) for r in results]
    if len(set(identities))!=len(identities): raise ValueError('Duplicate logical trial; refusing outcome selection')
    conversations=[r['conversation_id'] for r in results]
    if len(set(conversations))!=len(conversations): raise ValueError('Trials must use fresh conversation IDs')
    return results

def build():
    attempts=[json.loads(l) for l in (OUT/'provider_attempts.jsonl').read_text(encoding='utf-8').splitlines()]
    phases=['development20v5','repeat1','repeat2','repeat2_remaining','repeat3']
    results=normalize_trials([summarize(p,attempts) for phase in phases for p in sorted((OUT/'ui'/phase).glob('*.json'))])
    reviewed=load(OUT/'scope_reaudit.json')['rows'] if (OUT/'scope_reaudit.json').exists() else []
    by_call={(r['source_record'],r['task_id'],r['tool_call_id']):r['retrospective_verified'] for r in reviewed}
    for r in results:
        r['historical_contract_pass']=r['development_contract_pass']
        for s in r['scope_rows']:
            s['retrospective_scope_verified']=by_call.get((s['source_record'],s['task_id'],s['tool_call_id']))
        r['retrospective_sql_scope_pass']=all(s['retrospective_scope_verified'] for s in r['scope_rows']) if r['scope_rows'] and all(s['retrospective_scope_verified'] is not None for s in r['scope_rows']) else None
        if r['retrospective_sql_scope_pass'] is False:
            r['reasons']=sorted(set(r['reasons']+['retrospective_scope_refuted']))
            r['development_contract_pass']=False
    (OUT/'execution_audit.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    tables={'development_regression':[], 'repeated_runs':[], 'version_grounding_results':[], 'plan_protocol_results':[], 'sql_scope_results':[]}
    for r in results:
        row={k:r[k] for k in ['case_id','run','conversation_id','baseline_success','historical_contract_pass','development_contract_pass','retrospective_sql_scope_pass','tool_calls','tokens','provider_calls','total_system_latency_ms']}
        row.update(reasons=';'.join(r['reasons']),source_phase=r['source_phase'],source_record=r['source_record'],label=LABEL)
        tables['development_regression' if r['run']=='development20v5' else 'repeated_runs'].append(row)
        for t in r['turns']:
            base={'case_id':r['case_id'],'run':r['run'],'turn':t['turn'],'task_id':t['task_id'],'thread_id':t['thread_id'],'conversation_id':r['conversation_id'],'source_record':r['source_record']}
            tables['version_grounding_results'].append({**base,'dataset_version':t['scope'].get('dataset_version'),'dataset_version_id':t['scope'].get('dataset_version_id'),'split':t['scope'].get('split'), 'run_ids':json.dumps(t['binding'].get('run_ids',[])), 'ask_count':t['ask_count'],'suppressed_ask_count':t['suppressed_ask_count'],'clarification_required':r['clarification_required']})
            tables['plan_protocol_results'].append({**base,**{k:t[k] for k in ['plan_steps','completed_steps','plan_rejections','decision_rejections','replan_count']}})
        tables['sql_scope_results'].extend(r['scope_rows'])
    (OUT/'tables.json').write_text(json.dumps(tables,ensure_ascii=False,indent=2),encoding='utf-8')
    stable={c:{'passes':sum(r['development_contract_pass'] for r in results if r['case_id']==c and r['run'].startswith('repeat')),
              'runs':sum(r['case_id']==c and r['run'].startswith('repeat') for r in results),
              'failure_reasons':[r['reasons'] for r in results if r['case_id']==c and r['run'].startswith('repeat') and not r['development_contract_pass']]}
            for c in REPEATED}
    check=integrity()
    check.update(original_baseline='29/60 unchanged',repeated=stable,
        observed_development_cases=sum(r['run']=='development20v5' for r in results),observed_repeated_cases=sum(r['run'].startswith('repeat') for r in results))
    (OUT/'verification.json').write_text(json.dumps(check,indent=2),encoding='utf-8')
    write_markdown(results,check)
    post=[]
    for phase in ['post_branch_guard','post_schema_prerequisite']:
        for p in sorted((OUT/'ui'/phase).glob('*.json')):
            if load(p).get('finished_at'): post.append(summarize(p,attempts))
    (OUT/'post_measurement_checks.json').write_text(json.dumps(post,ensure_ascii=False,indent=2),encoding='utf-8')
    text='# Post-measurement local checks\n\n'+LABEL+'\n\nThese are separate diagnostic observations, never substituted into the 20+30 v5 denominators. v6 fixes independent-branch version proof; v7 also projects the existing actual-schema prerequisite into the model-visible callable tools/actions. No fixed schema tool/table/plan is installed; no Dispatcher/Registry/Security guard is weakened.\n\n'
    for r in post:
        text+=f'## {r["run"]} / {r["case_id"]}\n\nExecution-contract pass: {r["development_contract_pass"]}. Reasons: {r["reasons"]}. SQL executions: {r["successful_sql"]}; calls: {r["tool_calls"]}; measured tokens: {r["tokens"]}; system latency: {r["total_system_latency_ms"]} ms. Actual models: {r["actual_models"]}. Source: `{r["source_record"]}`.\n\n'
        text+='SQL results: '+json.dumps([s['rows'] for s in r['scope_rows']],ensure_ascii=False)+'\n\n'
    text+='The v6 UI check failed before executing SQL. The v7 check retrieved schema and executed SQL (train=2, total=30), but the total used unbound molecular_features; a coincidentally matching value is not scope proof. It emitted PLAN_RETAINED 25 times and reached the iteration limit. Final v9 rejects this SQL branch in a read-only re-audit and adds a regression; no additional LLM UI run was performed. New guard correctness is supported by targeted tests and retained real SQL parsing, not complete end-to-end recovery. Reliability acceptance remains open.\n'
    (OUT/'post_measurement_checks.md').write_text(text,encoding='utf-8')
    print(json.dumps({'phases':{p:{'runs':sum(r['run']==p for r in results),'passes':sum(r['run']==p and r['development_contract_pass'] for r in results)} for p in phases},'integrity':check},ensure_ascii=True))

if __name__=='__main__': build()
