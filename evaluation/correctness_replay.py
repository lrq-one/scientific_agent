"""Read-only recorded-state replay. No SQL execution, provider calls or scoring."""
from __future__ import annotations

import csv
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from app.agents.runtime import DecisionRuntime
from app.agents.goal_coverage import assess_goal_coverage
from app.models.schemas import AgentDecision, ScientificAgentState
from app.services.query_scope import validate_scope

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/phase4a_correctness_closure_20261009"
OLD = ROOT / "reports/phase4a_p0_hardening_20261008/ui"
PHASES = ["development20v5", "repeat1", "repeat2", "repeat2_remaining", "repeat3", "post_schema_prerequisite"]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for phase in PHASES:
        for path in sorted((OLD / phase).glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for turn in record.get("turns", []):
                finals = [event for event in turn.get("events", []) if event.get("event_type") == "FINAL_ANSWER"]
                saved = (finals[-1].get("payload_json", {}).get("state") or {}) if finals else {}
                if not saved:
                    continue
                if not {"user_id", "thread_id", "goal"} <= saved.keys():
                    rows.append({"source": str(path.relative_to(ROOT)), "task_id": (turn.get("task") or {}).get("id"),
                                 "check": "runtime_state", "status": "SKIPPED_EXPLANATION_SNAPSHOT",
                                 "new_tool_calls": 0, "new_llm_calls": 0,
                                 "details": "Follow-up FINAL stores a reduced snapshot, not a canonical runtime state"})
                    continue
                state = ScientificAgentState.model_validate(saved)
                identity = (state.task_id, state.thread_id, state.conversation_id)
                common = {"source": str(path.relative_to(ROOT)), "task_id": state.task_id,
                          "new_tool_calls": 0, "new_llm_calls": 0}
                coverage = assess_goal_coverage(state)
                rows.append({**common, "check": "goal_coverage", "status": coverage.status,
                             "details": coverage.model_dump_json()})
                for call, result in zip(state.tool_calls, state.observations):
                    if call.get("tool") != "execute_readonly_sql" or not result.success:
                        continue
                    try:
                        result_scope = validate_scope(result.metadata["sql"], result.metadata.get("params", {}),
                                                      state.query_scope, state.schema_cache)
                        status, detail = "PROVED_SUPPORTED_SCOPE", json.dumps(result_scope, ensure_ascii=False)
                    except (ValueError, KeyError) as exc:
                        status, detail = "REJECTED_OR_UNVERIFIED", str(exc)
                    rows.append({**common, "check": "executed_sql_scope", "status": status,
                                 "details": f"{call['tool_call_id']}: {detail}"})
                if any(event.get("event_type") == "PLAN_RETAINED" for event in turn.get("events", [])) and state.plan:
                    run = DecisionRuntime.__new__(DecisionRuntime)
                    run.owner = SimpleNamespace()
                    run._check = lambda config: None
                    emitted = []
                    run._emit = lambda config, event, message, **data: emitted.append(event)
                    state.no_progress_replan_count = 0
                    for _ in range(2):
                        run._apply_plan(state, AgentDecision(action="REPLAN", plan=state.plan), {})
                    ended = ScientificAgentState.model_validate(run.decision(run._return(state), {})["agent"])
                    bounded = ended.decision.action == "FINISH" and emitted == ["NO_PROGRESS_REPLAN"] * 2
                    assert (ended.task_id, ended.thread_id, ended.conversation_id) == identity
                    rows.append({**common, "check": "retained_plan_loop", "status": "BOUNDED" if bounded else "FAILED",
                                 "details": json.dumps({"emitted": emitted, "original_retained_events": sum(
                                     event.get("event_type") == "PLAN_RETAINED" for event in turn.get("events", [])),
                                     "ending": ended.decision.action, "identity_unchanged": True})})
    with (OUT / "replay_results.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=["source", "task_id", "check", "status", "new_tool_calls", "new_llm_calls", "details"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {"row_count": len(rows), "unique_tasks": len({row["task_id"] for row in rows}),
               "new_llm_calls": 0, "new_tool_calls": 0, "label": "OFFLINE REPLAY; NOT E2E OR NEW TASK SUCCESS",
               "counts": {check: {status: sum(row["check"] == check and row["status"] == status for row in rows)
                         for status in sorted({row["status"] for row in rows if row["check"] == check})}
                         for check in sorted({row["check"] for row in rows})}}
    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    sources = ["app/agents/runtime.py", "app/agents/plan_protocol.py", "app/agents/goal_coverage.py", "app/agents/decision_node.py",
               "app/models/schemas.py", "app/services/query_scope.py", "app/services/followup.py", "app/services/grounded_response.py",
               "app/tools/file_tools.py", "app/tools/registry.py", "app/api/routes.py", "tests/test_correctness_closure.py",
               "tests/test_p0_hardening.py", "tests/test_completion_gates.py", "tests/test_paired_model_comparison.py"]
    manifest = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    (OUT / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
