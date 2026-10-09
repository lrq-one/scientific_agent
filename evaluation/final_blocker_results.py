"""Offline accounting of capped UI and actual candidate SQL; no provider calls."""
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from app.agents.plan_protocol import satisfied
from app.models.schemas import ScientificAgentState
from app.services.query_scope import validate_scope

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/phase4a_final_blocker_resolution_20261009"
CASES = ["D09", "D08", "M02", "D06"]


def systemic(record):
    issues = []
    if record.get("cross_user_http_status") not in (None, 404):
        issues.append("cross_user_isolation")
    for turn in record.get("turns", []):
        if turn.get("reload_extra_posts"):
            issues.append("history_reload_execution")
        finals = [event for event in turn["events"] if event["event_type"] == "FINAL_ANSWER"]
        if not finals:
            continue
        saved = finals[-1]["payload_json"].get("state", {})
        state = ScientificAgentState.model_validate(saved)
        if state.no_progress_replan_count > 2:
            issues.append("no_progress_budget_exceeded")
        if any(event["event_type"] == "PLAN_RETAINED" for event in turn["events"]):
            issues.append("unexpected_plan_retained")
        if any(step.status == "completed" and not satisfied(step) for step in state.plan):
            issues.append("false_step_completion")
        for call, result in zip(state.tool_calls, state.observations):
            if call["tool"] == "text_to_sql" and not result.success and (
                "QueryScope" in (result.error or "") or result.failure_code == "UNVERIFIED_SCOPE"):
                if not result.metadata.get("sql_candidate"):
                    issues.append("scope_failure_lost_sql_candidate")
            if call["tool"] == "execute_readonly_sql" and result.success:
                try:
                    validate_scope(result.metadata["sql"], result.metadata["params"], state.query_scope, state.schema_cache)
                except (ValueError, KeyError):
                    issues.append("successful_sql_unproven_scope")
    return list(dict.fromkeys(issues))


def main():
    if "--check-record" in sys.argv:
        issues = systemic(json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8")))
        print(json.dumps({"systemic_issues": issues}))
        return 2 if issues else 0
    rows, details = [], {}
    for case in CASES:
        path = OUT / "ui" / f"blocker-{case}.json"
        if not path.exists():
            rows.append({"case_id": case, "status": "NOT_RUN"})
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        for turn in record["turns"]:
            finals = [event["payload_json"] for event in turn["events"] if event["event_type"] == "FINAL_ANSWER"]
            final = finals[-1] if finals else {}
            state = final.get("state", {})
            rows.append({"case_id": case, "conversation_id": record["conversation_id"],
                         "task_id": turn["task"]["id"], "status": turn["task"]["status"],
                         "quality": state.get("quality_status"), "coverage": state.get("goal_coverage", {}).get("status"),
                         "tools": sum(event["event_type"] == "TOOL_FINISHED" for event in turn["events"]),
                         "sql": sum(call["tool"] == "execute_readonly_sql" and result["success"] for call, result in zip(state.get("tool_calls", []), state.get("observations", []))),
                         "evidence": len(turn["evidence"]), "artifacts": len(turn["artifacts"]),
                         "fallback": any((event["payload_json"].get("llm_telemetry") or {}).get("fallback") for event in turn["events"]),
                         "latency_ms": turn.get("system_latency_ms"), "cross_user_status": record.get("cross_user_http_status"),
                         "reload_posts": turn.get("reload_extra_posts"), "systemic": ";".join(systemic(record))})
            details[case] = {"errors": state.get("errors"), "answer": state.get("final_answer"),
                             "query_scope": state.get("query_scope"),
                             "response_validations": final.get("llm_telemetry", {}).get("response_validations", []),
                             "failed_candidates": [result["metadata"] for call, result in zip(state.get("tool_calls", []), state.get("observations", []))
                                                   if call["tool"] == "text_to_sql" and not result["success"]]}
    with (OUT / "e2e_smoke.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["case_id", "conversation_id", "task_id", "status", "quality", "coverage", "tools", "sql", "evidence", "artifacts", "fallback", "latency_ms", "cross_user_status", "reload_posts", "systemic"])
        writer.writeheader(); writer.writerows(rows)
    attempts = [json.loads(line) for line in (OUT / "provider_attempts.jsonl").read_text(encoding="utf-8").splitlines()] if (OUT / "provider_attempts.jsonl").exists() else []
    starts = {item["attempt_id"] for item in attempts if item["phase"] == "provider_started"}
    ends = {item["attempt_id"]: item for item in attempts if item["phase"] == "provider_finished"}
    cost = {"conversations": len({row["conversation_id"] for row in rows if row.get("conversation_id")}),
            "new_qwen_attempts": len(starts), "finished": len(ends), "unfinished": len(starts - ends.keys()),
            "missing_usage": sum(not item.get("usage") for item in ends.values()),
            "input_tokens": sum((item.get("usage") or {}).get("prompt_tokens", 0) for item in ends.values()),
            "output_tokens": sum((item.get("usage") or {}).get("completion_tokens", 0) for item in ends.values()),
            "total_tokens": sum((item.get("usage") or {}).get("total_tokens", 0) for item in ends.values()),
            "actual_models": sorted({item.get("actual_model") for item in ends.values() if item.get("actual_model")}),
            "http_statuses": dict(Counter(str(item.get("http_status")) for item in ends.values())),
            "provider_latency_ms": round(sum(item["latency_ms"] for item in ends.values()), 2)}
    (OUT / "cost_summary.json").write_text(json.dumps(cost, indent=2), encoding="utf-8")
    (OUT / "candidate_and_response_diagnostics.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    from evaluation.final_blocker_audit import FILES
    sources = list(FILES) + ["app/agents/scientific_agent.py", "tests/test_final_blockers.py",
                            "evaluation/final_blocker_audit.py", "evaluation/final_blocker_results.py",
                            "scripts/run-final-blockers.ps1", "web/scripts/finalBlockers.mjs"]
    (OUT / "final_source_manifest.json").write_text(json.dumps({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}, indent=2), encoding="utf-8")
    # Keep captured UI outcome fields unchanged; only demonstrate final guard
    # behavior on recorded states, never count this as new execution success.
    from app.agents.runtime import DecisionRuntime
    from app.agents.scientific_agent import ScientificAgent
    from app.models.schemas import AgentDecision
    from types import SimpleNamespace
    replay = {}
    for case in ("D09", "D06", "M02"):
        record = json.loads((OUT / "ui" / f"blocker-{case}.json").read_text(encoding="utf-8"))
        turn = record["turns"][0]
        state = ScientificAgentState.model_validate([event["payload_json"]["state"] for event in turn["events"] if event["event_type"] == "FINAL_ANSWER"][-1])
        if case == "M02":
            replay[case] = {"before_quality_issues": state.quality_issues,
                            "after_offline_quality_issues": ScientificAgent._evidence_quality_issues(state)}
        else:
            decision = AgentDecision.model_validate(next(event["payload_json"]["decision"] for event in turn["events"] if event["event_type"] == "AGENT_DECISION"))
            state.plan, state.plan_id, state.schema_cache = [], None, {}
            run = DecisionRuntime.__new__(DecisionRuntime); run.owner = SimpleNamespace()
            try:
                run._apply_plan(state, decision, {})
            except ValueError as error:
                replay[case] = {"initial_proposal_rejected_before_install": True, "reason": str(error),
                                "state_unchanged": not state.plan and state.plan_id is None}
            else:
                raise AssertionError("Expected recorded deadlock rejection")
    replay["no_evidence_failure_contract"] = "ProcessOnlyResponse.claims maxItems=0; scientific invented claims remain reviewed/rejected"
    replay["new_llm_calls"] = 0; replay["new_tool_calls"] = 0
    (OUT / "post_smoke_replay.json").write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
    initial = json.loads((OUT / "initial_source_manifest.json").read_text(encoding="utf-8"))
    changed_old = [name for name, digest in initial.items() if name.startswith("reports/") and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    namespace = json.loads((ROOT / "reports/phase4a_p0_hardening_20261008/development_namespace.json").read_text(encoding="utf-8"))
    changed_frozen = [name for name, digest in namespace["original_report_hashes"].items() if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    from evaluation.p0_development import snapshot, DATABASE
    fingerprint = snapshot(DATABASE)
    assert not changed_old and not changed_frozen
    assert fingerprint == namespace["development_snapshot_sha256"]
    assert cost["conversations"] == 4 and cost["new_qwen_attempts"] == cost["finished"] == 38
    (OUT / "preservation_checks.json").write_text(json.dumps({"old_trace_changes": changed_old,
        "frozen_report_changes": changed_frozen, "scientific_snapshot_sha256": fingerprint,
        "snapshot_unchanged": True, "p2_ready": False, "new_paid_retries": 0}, indent=2), encoding="utf-8")
    git = subprocess.run([r"G:\Git\cmd\git.exe", "status", "--short"], cwd=ROOT, check=True, capture_output=True)
    (OUT / "git_status.txt").write_bytes(git.stdout)
    print(json.dumps(cost, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
