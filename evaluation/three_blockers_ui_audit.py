"""Read-only UI accounting. No LLM judge, DB writes, retries or benchmark grade."""
import json
import sys
from pathlib import Path

from app.agents.plan_protocol import satisfied
from app.models.schemas import ScientificAgentState
from app.services.query_scope import validate_scope


def audit(record):
    issues = []
    if record.get('cross_user_http_status') not in (None, 404):
        issues.append('cross_user_isolation')
    summary = {'case_id': record['case_id'], 'conversation_id': record.get('conversation_id'),
               'infrastructure_error': record.get('infrastructure_error')}
    for turn in record.get('turns', []):
        if turn.get('reload_extra_posts'):
            issues.append('history_reload_execution')
        finals = [e['payload_json'] for e in turn['events'] if e['event_type'] == 'FINAL_ANSWER']
        final = finals[-1] if finals else {}
        saved = final.get('state')
        measurements = []
        seen_usage = set()
        for event in turn['events']:
            if event['event_type'] not in {'INTENT_RESOLVED', 'AGENT_DECISION', 'DECISION_REJECTED', 'TOOL_FINISHED', 'FINAL_ANSWER'}:
                continue  # plan events repeat the same Decision measurement
            payload = event['payload_json']
            usage = payload.get('llm_telemetry')
            if event['event_type'] == 'TOOL_FINISHED':
                usage = (payload.get('result') or {}).get('metadata', {}).get('llm_telemetry')
            if usage and usage.get('llm_called'):
                signature = json.dumps(usage, sort_keys=True)
                if signature in seen_usage:
                    continue  # a service failure can echo last successful usage
                seen_usage.add(signature)
                measurements.append({'event_id': event['id'], 'event': event['event_type'], **usage})
        summary.update(task_id=turn['task']['id'], status=turn['task']['status'],
            latency_ms=turn['latency_ms'], persisted_evidence=len(turn['evidence']),
            artifacts=len(turn['artifacts']), reload_posts=turn.get('reload_extra_posts'),
            measurements=measurements, frontend_answer=turn.get('frontend_answer'))
        if saved:
            state = ScientificAgentState.model_validate(saved)
            summary.update(quality=state.quality_status, coverage=state.goal_coverage.model_dump(),
                sql=sum(c['tool'] == 'execute_readonly_sql' and r.success for c, r in zip(state.tool_calls, state.observations)),
                tools=state.tool_call_count, populations=[p.model_dump() for p in state.populations], errors=state.errors)
            if any(step.status == 'completed' and not satisfied(step) for step in state.plan):
                issues.append('false_step_completion')
            for call, result in zip(state.tool_calls, state.observations):
                if call['tool'] == 'execute_readonly_sql' and result.success:
                    try:
                        scope = state.query_scope.model_validate(call.get('query_scope') or state.query_scope.model_dump())
                        validate_scope(result.metadata['sql'], result.metadata.get('params', {}), scope, state.schema_cache)
                    except (ValueError, KeyError) as exc:
                        issues.append('successful_sql_unproven_scope:' + str(exc))
                    if state.populations:
                        population = next((p for p in state.populations if p.population_id == call.get('population_id')), None)
                        if not population or scope != population.query_scope:
                            issues.append('sql_population_scope_mismatch')
            for evidence in state.evidence:
                if evidence.source_type == 'database' and state.populations:
                    population = next((p for p in state.populations if p.population_id == evidence.population_id), None)
                    if not population or evidence.query_scope != population.query_scope:
                        issues.append('evidence_population_scope_mismatch')
        summary['fallback'] = any(m.get('fallback') for m in measurements)
        summary['measured_calls'] = sum(m.get('measured_llm_calls', 1) for m in measurements)
        summary['cost_basis'] = 'deduplicated reported product telemetry, not a provider billing ledger; unreported provider failures are not assigned fabricated usage'
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            summary[key] = sum(m[key] for m in measurements) if all(type(m.get(key)) is int for m in measurements) else None
        summary['provider_http_status'] = 'not captured by normal product telemetry; UI HTTP is recorded separately'
    summary['systemic_issues'] = sorted(set(issues))
    return summary


if __name__ == '__main__':
    result = audit(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    pause_file = Path(sys.argv[1]).parent / 'STOP_BEFORE_NEXT.txt'
    if pause_file.exists():
        result['acceptance_paused'] = pause_file.read_text(encoding='utf-8').strip()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(2 if result['systemic_issues'] else 3 if result.get('acceptance_paused') else 0)
