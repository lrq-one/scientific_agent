"""Account for a capped real UI smoke using persisted facts and HTTP telemetry."""
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

from app.agents.plan_protocol import satisfied
from app.models.schemas import ScientificAgentState
from app.services.query_scope import validate_scope

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/phase4a_correctness_closure_20261009"
CASES = ["D09", "D08", "D06", "M02", "U05", "H01"]


def systemic_issues(record):
    issues = []
    if record.get("cross_user_http_status") not in (None, 404):
        issues.append("cross_user_isolation")
    for turn in record.get("turns", []):
        events = turn.get("events", [])
        if sum(event.get("event_type") == "PLAN_RETAINED" for event in events) > 2:
            issues.append("repeated_plan_retained")
        finals = [event for event in events if event.get("event_type") == "FINAL_ANSWER"]
        saved = finals[-1].get("payload_json", {}).get("state") if finals else None
        if not saved or not {"user_id", "thread_id", "goal"} <= saved.keys():
            continue
        state = ScientificAgentState.model_validate(saved)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in state.requested_dimensions):
            issues.append("goal_dimension_contract_contains_non_column_descriptions")
        if state.no_progress_replan_count > 2:
            issues.append("no_progress_budget_exceeded")
        if any(step.status == "completed" and not satisfied(step) for step in state.plan):
            issues.append("completed_step_without_observation")
        for call, result in zip(state.tool_calls, state.observations):
            if call.get("tool") == "execute_readonly_sql" and result.success and result.metadata.get("scope_validation", {}).get("verified"):
                try:
                    validate_scope(result.metadata["sql"], result.metadata.get("params", {}), state.query_scope, state.schema_cache)
                except (ValueError, KeyError) as exc:
                    issues.append("scope_false_positive: " + str(exc))
    return list(dict.fromkeys(issues))


def main():
    if "--check-record" in sys.argv:
        record = json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8"))
        issues = systemic_issues(record)
        # Stop the capped runner when a systemic contract issue is discovered
        # in a just-completed earlier record too; never rerun a paid task.
        for previous in (OUT / "ui").glob("closure-*.json"):
            issues.extend(systemic_issues(json.loads(previous.read_text(encoding="utf-8"))))
        issues = list(dict.fromkeys(issues))
        print(json.dumps({"systemic_issues": issues}))
        return 2 if issues else 0
    rows = []
    for case in CASES:
        path = OUT / "ui" / f"closure-{case}.json"
        if not path.exists():
            rows.append({"case_id": case, "turn": "", "status": "NOT_RUN", "reason": "capped smoke stopped/not started"})
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        for number, turn in enumerate(record.get("turns", []), 1):
            events = turn.get("events", [])
            finals = [event for event in events if event.get("event_type") == "FINAL_ANSWER"]
            final = finals[-1].get("payload_json", {}) if finals else {}
            state = final.get("state") or {}
            telemetry = [event.get("payload_json", {}).get("llm_telemetry") or {} for event in events]
            task = turn.get("task") or {}
            rows.append({"case_id": case, "turn": number, "conversation_id": record.get("conversation_id"),
                "task_id": task.get("id"), "thread_id": task.get("thread_id"), "status": task.get("status"),
                "quality_status": state.get("quality_status", final.get("quality_status")),
                "goal_coverage": (state.get("goal_coverage") or {}).get("status", "NOT_ASSESSED"),
                "tool_calls": sum(event.get("event_type") == "TOOL_FINISHED" for event in events),
                "evidence_count": len(turn.get("evidence", [])), "artifacts_count": len(turn.get("artifacts", [])),
                "sql_executions": sum(event.get("event_type") == "TOOL_FINISHED" and
                    event.get("payload_json", {}).get("tool") == "execute_readonly_sql" and
                    event.get("payload_json", {}).get("result", {}).get("success") is True for event in events),
                "fallback": any(item.get("fallback") for item in telemetry), "latency_ms": turn.get("system_latency_ms"),
                "cross_user_http_status": record.get("cross_user_http_status"),
                "history_reload_posts": turn.get("reload_extra_posts"),
                "reason": record.get("infrastructure_error") or "; ".join(systemic_issues(record))})
        if not record.get("turns"):
            rows.append({"case_id": case, "turn": "", "status": "INFRASTRUCTURE_FAILED", "reason": record.get("infrastructure_error")})
    columns = ["case_id", "turn", "conversation_id", "task_id", "thread_id", "status", "quality_status", "goal_coverage",
               "tool_calls", "sql_executions", "evidence_count", "artifacts_count", "fallback", "latency_ms",
               "cross_user_http_status", "history_reload_posts", "reason"]
    with (OUT / "e2e_smoke.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, columns)
        writer.writeheader()
        writer.writerows(rows)
    telemetry_file = OUT / "provider_attempts.jsonl"
    telemetry = [json.loads(line) for line in telemetry_file.read_text(encoding="utf-8").splitlines()] if telemetry_file.exists() else []
    starts = {item["attempt_id"]: item for item in telemetry if item["phase"] == "provider_started"}
    finishes = {item["attempt_id"]: item for item in telemetry if item["phase"] == "provider_finished"}
    summary = {"conversations": len({row.get("conversation_id") for row in rows if row.get("conversation_id")}),
               "turns": sum(bool(row.get("turn")) for row in rows), "new_qwen_attempts": len(starts),
               "finished_attempts": len(finishes), "missing_usage": sum(not item.get("usage") for item in finishes.values()),
               "unfinished_attempts": len(starts.keys() - finishes.keys()),
               "input_tokens": sum((item.get("usage") or {}).get("prompt_tokens", 0) for item in finishes.values()),
               "output_tokens": sum((item.get("usage") or {}).get("completion_tokens", 0) for item in finishes.values()),
               "total_tokens": sum((item.get("usage") or {}).get("total_tokens", 0) for item in finishes.values()),
               "provider_latency_ms": round(sum(item.get("latency_ms", 0) for item in finishes.values()), 2),
               "http_statuses": dict(Counter(str(item.get("http_status")) for item in finishes.values())),
               "actual_models": sorted({item["actual_model"] for item in finishes.values() if item.get("actual_model")}),
               "task_statuses": dict(Counter(row["status"] for row in rows)), "not_a_new_frozen_score": True}
    (OUT / "cost_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
