"""Read-only development execution audit. Never grade/overwrite Frozen records."""
from pathlib import Path
import json
import hashlib
from collections import Counter
from evaluation.p0_development import snapshot, DATABASE

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase4a_p0_hardening_20261008'

def load(path):
    return json.loads(path.read_text(encoding='utf-8'))

def audit(record):
    turns = []
    for i, t in enumerate(record.get('turns', [])):
        events = t.get('events', [])
        states = [e['payload_json'].get('state', {}) for e in events if e['event_type']=='FINAL_ANSWER']
        state = states[-1] if states else {}
        executed = [e['payload_json'] for e in events if e['event_type']=='TOOL_FINISHED']
        sql = [e for e in executed if e['tool']=='execute_readonly_sql' and e['result']['success']]
        telemetry = [e['payload_json'].get('llm_telemetry', {}) for e in events]
        reasons = []
        if t.get('task', {}).get('status') != 'completed': reasons.append('task_not_completed')
        if state.get('quality_status') in {'EXECUTION_FAILED', 'CONFLICTING_EVIDENCE'}: reasons.append(state['quality_status'])
        if any(x.get('fallback') for x in telemetry): reasons.append('fallback')
        if t.get('frontend_errors'): reasons.append('frontend_error')
        if any(not e['result'].get('metadata', {}).get('scope_validation', {}).get('verified') for e in sql): reasons.append('executed_scope_unverified')
        answers = '\n'.join(t.get('frontend_answer', []))
        if not answers.strip(): reasons.append('empty_UI_answer')
        if t.get('reload_extra_posts'): reasons.append('history_reload_executed_POST')
        if t.get('reload_answer') != t.get('frontend_answer'): reasons.append('history_answer_changed_after_reload')
        for wait in t.get('waits', []):
            for key in ('id','thread_id','conversation_id'):
                if wait.get('task', {}).get(key) != t.get('task', {}).get(key): reasons.append('HITL_identity_changed_'+key)
        if any(h['status']>=500 for h in record.get('http', [])): reasons.append('HTTP_5xx')
        # Status is only a runtime outcome, not scientific goal proof. The final
        # semantic review is recorded independently by a human-readable audit.
        turns.append({'turn':i+1, 'runtime_pass':not reasons, 'reasons':reasons,
            'task_id': t.get('task', {}).get('id'), 'thread_id':t.get('task', {}).get('thread_id'),
            'scope':t.get('task', {}).get('intent_json', {}).get('query_scope', {}),
            'binding':t.get('task', {}).get('intent_json', {}).get('resource_binding', {}),
            'quality_status':state.get('quality_status'),
            'evidence_count':len(t.get('evidence', [])), 'tool_calls':len(executed),
            'successful_sql_count':len(sql), 'sql':sql,
            'plan_steps':len(state.get('plan', [])),
            'completed_steps':sum(s['status']=='completed' for s in state.get('plan', [])),
            'plan_rejections':sum(e['event_type']=='PLAN_REJECTED' for e in events),
            'decision_rejections':sum(e['event_type']=='DECISION_REJECTED' for e in events),
            'replan_count':state.get('replan_count', 0),
            'ask_count':sum(e['event_type']=='WAITING_FOR_USER' for e in events),
            'suppressed_ask_count':sum(e['event_type']=='ASK_SUPPRESSED' for e in events),
            'latency_ms':t.get('system_latency_ms', t.get('latency_ms')),
            'final_errors':state.get('errors', []), 'answer':answers})
    return {'case_id':record['case_id'], 'source_case':record.get('development_source_case_id'),
            'conversation_id':record.get('conversation_id'), 'turns':turns,
            'runtime_pass':bool(turns) and all(t['runtime_pass'] for t in turns)}

def integrity():
    original = load(OUT/'development_namespace.json')
    changed = [p for p,h in original['original_report_hashes'].items()
               if not (ROOT/p).is_file() or hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    frozen = load(ROOT/'reports/phase4_v2_20261008/freeze_manifest.json')
    protected = {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in frozen['source_hashes'].items()
        if any(x in p for x in ['sql_guard','security_policy','checkpointing','grounded_response','object_storage','registry','skills.py'])}
    return {'original_report_changed_files':changed, 'protected_source_files_unchanged':protected,
            'original_scientific_snapshot_unchanged':snapshot('phase4_eval_v2')==original['frozen_scientific_snapshot_sha256'],
            'development_scientific_snapshot_unchanged':snapshot(DATABASE)==original['frozen_scientific_snapshot_sha256']}

def capture_code(version='v5'):
    if version not in {'v5','v6','v7','v8','v9'}: raise ValueError('Unsupported development source version')
    destination=OUT/f'development_code_version_{version}.json'
    if destination.exists(): raise RuntimeError('Refusing overwrite of development code version')
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'app').rglob('*.py')}
    hashes['web/scripts/p0Development.mjs']=hashlib.sha256((ROOT/'web/scripts/p0Development.mjs').read_bytes()).hexdigest()
    destination.write_text(json.dumps({'label':'development only; not a frozen release','source_hashes':hashes},indent=2),encoding='utf-8')
    print('Development product source version captured')

if __name__=='__main__':
    import sys
    if '--capture-code=v9' in sys.argv: capture_code('v9')
    elif '--capture-code=v8' in sys.argv: capture_code('v8')
    elif '--capture-code=v7' in sys.argv: capture_code('v7')
    elif '--capture-code=v6' in sys.argv: capture_code('v6')
    elif '--capture-code' in sys.argv: capture_code()
    elif '--integrity' in sys.argv: print(json.dumps(integrity()))
    else:
        for folder in sys.argv[1:]:
            results=[audit(load(p)) for p in (OUT/'ui'/folder).glob('*.json')]
            print(json.dumps({'phase':folder,'cases':len(results),'runtime_pass':sum(r['runtime_pass'] for r in results),
                'failures':{r['source_case']:[t['reasons'] for t in r['turns']] for r in results if not r['runtime_pass']}},ensure_ascii=True))
