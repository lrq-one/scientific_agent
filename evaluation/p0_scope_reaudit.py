"""Read-only retrospective scope review; never rewrite historical ToolResults."""
import json
import psycopg
from evaluation.p0_audit import OUT,load
from evaluation.p0_development import TABLES,DATABASE
from app.models.schemas import QueryScope
from app.services.query_scope import validate_scope


def review():
    schema={}
    with psycopg.connect('postgresql://scientific:scientific@127.0.0.1:55432/'+DATABASE) as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        for table,column,typ in connection.execute(
            "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=ANY(%s)",(TABLES,)):
            schema.setdefault(table,[]).append({'name':column,'type':typ})
    rows=[]
    records=load(OUT/'execution_audit.json')
    if (OUT/'post_measurement_checks.json').exists(): records+=load(OUT/'post_measurement_checks.json')
    for record in records:
        for turn in record['turns']:
            for call in turn['sql']:
                metadata=call['result']['metadata']
                previous=metadata.get('scope_validation',{})
                scope=QueryScope.model_validate(previous.get('query_scope',turn['scope']))
                error=None
                try: validate_scope(metadata['sql'],metadata['params'],scope,schema)
                except ValueError as exc: error=str(exc)
                rows.append({'case_id':record['case_id'],'run':record['run'],'task_id':turn['task_id'],
                    'tool_call_id':call.get('tool_call_id'),'historical_verified':previous.get('verified'),
                    'retrospective_verified':error is None,'error':error,'source_record':record['source_record']})
    output={'label':'NOT FROZEN TEST / NOT UNSEEN EVALUATION',
        'method':'final v9 validation of retained v5 and diagnostic SQL/params against unchanged authorized scientific schema; no SQL execution, no LLM',
        'guard_source_capture':'development_code_version_v9.json',
        'rows':rows}
    (OUT/'scope_reaudit.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'executions':len(rows),'refuted':[r for r in rows if not r['retrospective_verified']]}))


if __name__=='__main__': review()
