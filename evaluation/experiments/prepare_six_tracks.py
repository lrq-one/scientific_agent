"""Build the six-track experiment package from existing, immutable fixtures.

The script copies case/gold provenance into new experiment directories and
records measured references without manufacturing missing live metrics.  It
never touches ``evaluation/runs`` or frozen benchmark outputs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evaluation" / "experiments"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def copy_cases(source: Path, target: Path, limit: int | None = None) -> list[dict]:
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        rows = rows[:limit]
    target.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return rows


def write_gold(rows: list[dict], target: Path, fields: tuple[str, ...]) -> None:
    gold = []
    for row in rows:
        gold.append({"case_id": row.get("case_id"), **{field: row.get(field) for field in fields if field in row},
                     "label_status": row.get("review_status", "source_fixture")})
    target.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in gold) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_track(spec: dict) -> None:
    directory = OUT / spec["id"]
    (directory / "runs" / "baseline").mkdir(parents=True, exist_ok=True)
    (directory / "runs" / "optimized").mkdir(parents=True, exist_ok=True)
    (directory / "reports").mkdir(parents=True, exist_ok=True)
    rows = copy_cases(ROOT / spec["cases_source"], directory / "cases.jsonl", spec.get("case_limit"))
    write_gold(rows, directory / "gold.jsonl", tuple(spec.get("gold_fields", ())))
    cases_hash = sha256(directory / "cases.jsonl")
    gold_hash = sha256(directory / "gold.jsonl")
    current_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {
        "schema_version": "scientific-agent-six-track-v1",
        "track_id": spec["id"],
        "title": spec["title"],
        "baseline": spec["baseline"],
        "optimized": {"sha": current_sha, "code_path": spec["code_path"]},
        "cases": {"count": len(rows), "path": "cases.jsonl", "sha256": cases_hash, "source": spec["cases_source"]},
        "gold": {"path": "gold.jsonl", "sha256": gold_hash, "provenance": spec.get("gold_provenance", "source fixture review_status; independent label provenance retained in source file"), "split": spec["split"]},
        "environment": {
            "provider": spec.get("provider", "offline_or_historical_reference"),
            "database_writes": False, "minio_writes": False, "frozen_outputs_modified": False,
            "model_calls_this_batch": 0,
        },
        "measurement_policy": spec["measurement_policy"],
        "limitations": spec["limitations"],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for side in ("baseline", "optimized"):
        payload = dict(spec["metrics"].get(side, {}))
        payload.update({"track_id": spec["id"], "run_label": side, "case_count": len(rows), "cases_sha256": cases_hash,
                        "gold_sha256": gold_hash, "measurement_status": payload.get("measurement_status", "not_measured")})
        (directory / "runs" / side / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paired_rows = spec.get("paired", [])
    with (directory / "paired.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "baseline", "optimized", "delta", "status", "source"])
        writer.writeheader()
        writer.writerows(paired_rows)
    (directory / "ablation.json").write_text(json.dumps(spec.get("ablation", {"status": "not_measured"}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "failure_analysis.json").write_text(json.dumps(spec["failure_analysis"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = [f"# {spec['title']}", "", f"Cases: {len(rows)}; split: {spec['split']}.",
              f"Baseline: {spec['baseline']['sha']} ({spec['baseline']['mechanism']}).",
              f"Optimized code: {spec['code_path']}.", "", "## Measurement boundary", "", spec["measurement_policy"], "",
              "## Limitations", "", *[f"- {item}" for item in spec["limitations"]], "", "## Failure analysis", ""]
    report.extend(f"- {item}" for item in spec["failure_analysis"].get("findings", []))
    (directory / "reports" / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    specs = [
        {
            "id": "01_skill_prompt_context", "title": "Skill Routing + Prompt + Context",
            "cases_source": "evaluation/skill_routing/cases.jsonl", "case_limit": 150,
            "gold_fields": ("gold_skills",), "split": "dev+test_source_fixture",
            "baseline": {"sha": "ff890d0", "mechanism": "all-catalog routing and inline prompts", "historical_metric_sha": "not_recorded_in_phase4_run_manifest"},
            "code_path": "app/services/skills.py; app/services/prompt_catalog.py; app/services/context_projection.py",
            "provider": "historical_qwen_reference_and_offline_current_code",
            "measurement_policy": "Historical Skill runs are referenced, not rerun. Current prompt/catalog/context changes are validated offline; no current paired Qwen delta is claimed.",
            "limitations": ["Historical skill configs do not persist a Git SHA for every run.", "Current six-track pairing has zero new provider calls; live quality/cost remains not_measured."],
            "metrics": {"baseline": {"measurement_status": "historical_reference", "source": "evaluation/runs/skill_dev_s0_all9/metrics.json", "cases": 100, "top1_accuracy": 1.0, "macro_f1": 0.8935927419, "total_tokens": 273820, "p95_latency_ms": 27197.111999999998}, "optimized": {"measurement_status": "offline_current_code_not_paired", "source": "tests/test_six_track_protocols.py", "routing_calls": "not_measured", "context_compression_ratio": "not_measured"}},
            "paired": [{"metric": "top1_accuracy", "baseline": 1.0, "optimized": "not_measured", "delta": "not_measured", "status": "not_comparable", "source": "historical vs current"}],
            "ablation": {"status": "historical_reference", "source": "evaluation/runs/skill_test_s0_all9_nothinking/metrics.json", "no_skill_live": "evaluation/benchmark/results/live/live_comparison.json"},
            "failure_analysis": {"findings": ["BM25 k=3 reduced input tokens but lowered retrieval recall in the existing 100-case run.", "No-skill live ablation matched optimized strict success on 10 cases, so Skill benefit is unproven."]},
        },
        {
            "id": "02_goal_plan", "title": "GoalContract + PlanStep", "cases_source": "evaluation/benchmark/cases_extended.jsonl", "case_limit": 34,
            "gold_fields": ("family", "expected_outcome"), "split": "replay_protocol",
            "baseline": {"sha": "66909da", "mechanism": "pre-immutable GoalContract implementation / legacy completion paths"},
            "code_path": "app/agents/goal_contract.py; app/agents/plan_protocol.py; app/models/schemas.py",
            "measurement_policy": "Deterministic protocol tests are the current evidence. A 34-case full paired metric run is not yet measured; test pass counts are not converted into task success claims.",
            "limitations": ["No independent semantic re-labeling of a new 34-case holdout in this batch.", "Historical A/B/C production records remain read-only references."],
            "metrics": {"baseline": {"measurement_status": "protocol_reference", "false_success_rate": "not_measured", "false_extra_obligation_rate": "not_measured"}, "optimized": {"measurement_status": "offline_contract_tests", "goal_contract_tests": "passed", "planstep_tests": "passed", "false_success_rate": "not_measured"}},
            "paired": [{"metric": "false_extra_obligation_rate", "baseline": "not_measured", "optimized": "not_measured", "delta": "not_measured", "status": "not_measured", "source": "requires independent gold adjudication"}],
            "ablation": {"status": "planned", "variants": ["GoalContract only", "GoalContract+PlanStep", "negative Evidence deletion"]},
            "failure_analysis": {"findings": ["Current runtime freezes user goals and rejects plan-only obligations.", "Independent semantic false-success measurement is still required before claiming improvement."]},
        },
        {
            "id": "03_recovery_executor", "title": "RecoveryPolicy + Deterministic Executor", "cases_source": "evaluation/v2/scenario_cases.jsonl", "case_limit": 30,
            "gold_fields": ("family", "expected_outcome"), "split": "dev_draft_protocol",
            "baseline": {"sha": "d0a9d6b", "mechanism": "unified recovery was not yet bounded by current failure codes"},
            "code_path": "app/services/recovery.py; app/agents/runtime.py",
            "measurement_policy": "Fault-injection coverage is represented by existing tests and C live traces. No new Qwen or database run was executed.",
            "limitations": ["scenario_cases labels are agent_drafted and not a frozen holdout.", "Recovery latency/cost distributions are not measured for this package."],
            "metrics": {"baseline": {"measurement_status": "historical_reference", "recovery_success_rate": "not_measured", "no_progress_policy": "unbounded_or_legacy"}, "optimized": {"measurement_status": "deterministic_tests_and_C_trace", "directed_scope_repair_limit": 1, "no_progress_replan_limit": 2, "invalid_tool_call_rate": "not_measured"}},
            "paired": [{"metric": "scope_repair_limit", "baseline": "not_measured", "optimized": 1, "delta": "bounded", "status": "protocol_fact", "source": "app/agents/runtime.py"}],
            "ablation": {"status": "planned", "variants": ["directed repair", "same-plan replan", "safe stop"]},
            "failure_analysis": {"findings": ["C now records diagnostic candidate, scope reason and one directed repair budget.", "No-progress termination is enforced, but full fault-injection matrix still needs a dedicated runner."]},
        },
        {
            "id": "04_text2sql_scope", "title": "Text2SQL + QueryScope", "cases_source": "evaluation/text2sql/cases.jsonl", "case_limit": 100,
            "gold_fields": ("gold_tables", "gold_sql", "expected_result_signature"), "split": "dev+test_source_fixture",
            "baseline": {"sha": "d4b3a65", "mechanism": "pre-lineage QueryScope/SQLCandidate recovery"},
            "code_path": "app/services/text2sql.py; app/services/query_scope.py; app/services/query_decomposition.py",
            "provider": "historical_qwen_and_isolated_postgres_references",
            "measurement_policy": "Existing 70-case Qwen Text2SQL and focused C live runs are preserved. New decomposition/lineage checks are offline only; no scope relaxation or database writes.",
            "limitations": ["C remains a safe UNVERIFIED_SCOPE rejection in the focused live trace.", "Complex JOIN proof coverage needs additional independently labeled SQL cases."],
            "metrics": {"baseline": {"measurement_status": "historical_reference", "source": "evaluation/runs/text2sql_dev_q0_gold_schema_output_contract_v2/metrics.json", "cases": 70, "result_accuracy": 0.9285714286, "security_pass_rate": 1.0}, "optimized": {"measurement_status": "offline_current_code_plus_C_trace", "scope_decomposition_lineage": "measured_by_unit_test", "C_strict_success_rate": 0.0, "scope_false_accept_rate": 0.0, "scope_false_reject_rate": "not_measured"}},
            "paired": [{"metric": "scope_false_accept_rate", "baseline": "not_measured", "optimized": 0.0, "delta": "not_comparable", "status": "safety_floor", "source": "focused C trace + tests"}],
            "ablation": {"status": "historical_reference", "source": "evaluation/runs/text2sql_dev_q3_repair_q1_bm25_failures_output_contract_v2/metrics.json", "repair_success_rate": 0.0},
            "failure_analysis": {"findings": ["UNVERIFIED_SCOPE is distinct from SCOPE_VIOLATION and remains a safe rejection.", "model_run→experiment→dataset_version and training_memberships→dataset_version are now audited before recommending independent populations."]},
        },
        {
            "id": "05_evidence_artifact", "title": "Evidence + Artifact", "cases_source": "evaluation/benchmark/cases_extended.jsonl", "case_limit": 34,
            "gold_fields": ("family", "expected_outcome", "requires_artifact"), "split": "replay_protocol",
            "baseline": {"sha": "1802835", "mechanism": "pre-evidence/artifact guard lineage"},
            "code_path": "app/services/grounded_response.py; app/services/artifacts.py",
            "measurement_policy": "Existing grounded-response and isolated MinIO tests are the evidence. Artifact byte hashes/columns are now persisted in generated artifact metadata; no production object was touched.",
            "limitations": ["Independent 34-case evidence claim adjudication is not measured in this batch.", "MinIO verification metrics remain isolated-test only."],
            "metrics": {"baseline": {"measurement_status": "historical_reference", "evidence_coverage": "not_measured", "artifact_correctness": "not_measured"}, "optimized": {"measurement_status": "offline_and_isolated_tests", "claim_evidence_gate": "tested", "artifact_sha256": "implemented", "artifact_column_manifest": "implemented"}},
            "paired": [{"metric": "artifact_hash_manifest", "baseline": "absent", "optimized": "present", "delta": "protocol improvement", "status": "code_inspection", "source": "app/services/artifacts.py"}],
            "ablation": {"status": "planned", "variants": ["evidence gate on", "evidence gate off", "artifact bytes tampered"]},
            "failure_analysis": {"findings": ["Unsupported claims are rejected by persisted Evidence IDs.", "A failed export remains non-artifact; generated outputs now carry byte and column metadata for verification."]},
        },
        {
            "id": "06_hitl_sse", "title": "HITL + Checkpoint + SSE", "cases_source": "evaluation/e2e/cases.jsonl", "case_limit": 20,
            "gold_fields": ("required_trace", "requires_artifact", "hitl_required"), "split": "isolated_e2e_source_fixture",
            "baseline": {"sha": "3ed25de", "mechanism": "pre-durable HITL/SSE lifecycle"},
            "code_path": "app/services/checkpointing.py; app/api/routes.py; web/src/executionState.js; web/src/executionStages.js",
            "measurement_policy": "Backend isolated integration and frontend/browser tests are preserved. A new reconnect/fault-point distribution is not measured.",
            "limitations": ["The source E2E set has 20 cases, below the preferred 30–50; this is reported rather than padded with duplicates.", "Browser opt-in tests are not claimed as run when skipped by environment."],
            "metrics": {"baseline": {"measurement_status": "historical_reference", "resume_success_rate": "not_measured", "reconnect_dedup": "not_measured"}, "optimized": {"measurement_status": "isolated_e2e_and_frontend_tests", "backend_integration_cases": 13, "frontend_unit_cases": 21, "browser_e2e": "1 passed, 2 skipped", "reconnect_dedup": "not_measured"}},
            "paired": [{"metric": "event_audit_preservation", "baseline": "not_measured", "optimized": "preserved", "delta": "protocol fact", "status": "code_and_tests", "source": "routes.py / conversation history"}],
            "ablation": {"status": "planned", "variants": ["disconnect/replay", "duplicate event delivery", "cancel race", "wrong identity resume"]},
            "failure_analysis": {"findings": ["Persisted event IDs are replayed after a cursor; UI stage grouping does not delete audit events.", "Reconnection and duplicate suppression need a dedicated browser fault-injection runner before claiming a rate."]},
        },
        {
            "id": "07_end_to_end", "title": "End-to-end controlled delivery", "cases_source": "evaluation/benchmark/cases_extended.jsonl", "case_limit": 34,
            "gold_fields": ("family", "expected_outcome"), "split": "replay_plus_live_reference",
            "baseline": {"sha": "209fda8", "mechanism": "engineering/intelligence baseline used by ten-case live comparison"},
            "code_path": "app/agents/runtime.py; app/api/routes.py; web/src/executionStages.js",
            "provider": "real_qwen_reference_no_new_calls",
            "measurement_policy": "Only the committed 10-case real-Qwen baseline/optimized/no-skill artifacts and existing isolated E2E runs are used. The 34-case package is a reproducibility scaffold, not a new success claim.",
            "limitations": ["Ten-case live comparison is insufficient to generalize six-track intelligence gains.", "No new Qwen calls were made because the prior ledger has 189/250 provider calls and the remaining budget is reserved for a separately approved paired run."],
            "metrics": {"baseline": {"measurement_status": "real_qwen_reference", "source": "evaluation/benchmark/runs/baseline-live-20261010/metrics.json", "strict_task_success": 0.6, "provider_calls": 55, "p95_latency_ms": 21213.86}, "optimized": {"measurement_status": "real_qwen_reference", "source": "evaluation/benchmark/runs/optimized-live-20261010/metrics.json", "strict_task_success": 0.7, "provider_calls": 58, "p95_latency_ms": 22928.77}},
            "paired": [{"metric": "strict_task_success", "baseline": 0.6, "optimized": 0.7, "delta": 0.1, "status": "real_qwen_10_case_reference", "source": "committed live comparison"}, {"metric": "provider_calls", "baseline": 55, "optimized": 58, "delta": 3, "status": "real_qwen_10_case_reference", "source": "nested call ledger"}, {"metric": "p95_latency_ms", "baseline": 21213.86, "optimized": 22928.77, "delta": 1714.91, "status": "real_qwen_10_case_reference", "source": "committed live comparison"}],
            "ablation": {"status": "real_qwen_reference", "source": "evaluation/benchmark/runs/ablation-no-skill-20261010-2/metrics.json", "strict_task_success": 0.7, "provider_calls": 58},
            "failure_analysis": {"findings": ["Optimized strict success increased by one case in the committed 10-case run, while provider calls and P95 latency increased.", "No-skill matched optimized strict success; attribution to Skill/Prompt/Context is not established."]},
        },
    ]
    for spec in specs:
        write_track(spec)
    print(json.dumps({"tracks": len(specs), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
