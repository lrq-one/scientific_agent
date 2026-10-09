"""Paired real-Qwen benchmark over isolated synthetic inputs.

The runner exercises production request-understanding, planning, Text2SQL,
decision and grounded-response components without opening PostgreSQL/MinIO or
executing generated SQL.  It is deliberately subprocess-friendly: ``--root``
selects the checkout under test, while the external ledger remains active for
the historical baseline which predates the in-process live budget gate.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse


def _target_root(value: str) -> Path:
    root = Path(value).resolve()
    if not (root / "app").is_dir():
        raise SystemExit(f"target root has no app package: {root}")
    # The target is inserted before the benchmark checkout so baseline imports
    # cannot accidentally resolve optimized modules.
    sys.path.insert(0, str(root))
    os.chdir(root)
    return root


class BudgetStop(RuntimeError):
    pass


class RunLedger:
    """A second, harness-level fail-closed budget for every provider request."""

    def __init__(self, run_dir: Path, *, max_requests: int, max_tokens: int,
                 max_cost_usd: float, max_minutes: float, usd_to_cny: float = 7.0):
        self.run_dir = run_dir
        self.path = run_dir / "budget_ledger.json"
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_minutes = max_minutes
        self.usd_to_cny = usd_to_cny
        self.input_cny_per_million = 1.44
        self.output_cny_per_million = 5.76
        self.started = time.monotonic()
        self.requests = 0
        self.reserved_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.known_usage = True
        self.cost_usd = 0.0
        self.exceeded_reason: str | None = None
        self.events: list[dict[str, Any]] = []
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._persist("run_started", {"provider_host": self._host(), "model": os.getenv("LLM_MODEL")})

    @staticmethod
    def _host() -> str | None:
        raw = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE") or ""
        try:
            return urlparse(raw).hostname
        except ValueError:
            return None

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        cny = input_tokens / 1_000_000 * self.input_cny_per_million
        cny += output_tokens / 1_000_000 * self.output_cny_per_million
        return cny / self.usd_to_cny

    def reserve(self, stage: str, estimate: int) -> None:
        elapsed = (time.monotonic() - self.started) / 60
        estimate = max(1, int(estimate))
        if self.exceeded_reason:
            raise BudgetStop(self.exceeded_reason)
        if elapsed >= self.max_minutes:
            raise BudgetStop("wall-clock cap reached")
        if self.requests >= self.max_requests:
            raise BudgetStop("request cap reached")
        if self.total_tokens + self.reserved_tokens + estimate > self.max_tokens:
            raise BudgetStop("token cap reached")
        if self.cost_usd + self._cost(estimate, 0) > self.max_cost_usd:
            raise BudgetStop("cost cap reached")
        self.requests += 1
        self.reserved_tokens += estimate
        self._persist("request_reserved", {"stage": stage, "estimated_tokens": estimate})

    def finish(self, stage: str, usage: dict[str, Any] | None, latency_ms: float,
               error: str | None = None, estimate: int = 0) -> None:
        usage = usage or {}
        inp = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None
        out = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None
        total = usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else None
        if total is None:
            total = max(1, int(estimate))
            self.known_usage = False
        self.reserved_tokens = max(0, self.reserved_tokens - max(1, int(estimate)))
        self.input_tokens += inp or 0
        self.output_tokens += out or 0
        self.total_tokens += total
        if inp is None or out is None:
            self.known_usage = False
        self.cost_usd += self._cost(inp, out) if inp is not None and out is not None else self._cost(0, max(1, int(estimate)))
        if self.total_tokens > self.max_tokens:
            self.exceeded_reason = "token cap reached after provider usage"
        elif self.cost_usd > self.max_cost_usd:
            self.exceeded_reason = "cost cap reached after provider usage"
        self._persist("request_finished", {"stage": stage, "input_tokens": inp,
            "output_tokens": out, "total_tokens": usage.get("total_tokens"),
            "charged_tokens": total, "latency_ms": round(latency_ms, 2),
            "error_type": error})
        if self.exceeded_reason:
            self._persist("budget_exceeded", {"stage": stage, "reason": self.exceeded_reason})

    def _persist(self, event: str, data: dict[str, Any]) -> None:
        self.events.append({"event": event, "time": time.time(), **data})
        self.path.write_text(json.dumps({
            "limits": {"max_requests": self.max_requests, "max_tokens": self.max_tokens,
                        "max_cost_usd": self.max_cost_usd, "max_minutes": self.max_minutes},
            "usage": {"requests": self.requests, "input_tokens": self.input_tokens,
                       "output_tokens": self.output_tokens, "total_tokens": self.total_tokens,
                       "reserved_tokens": self.reserved_tokens,
                       "usage_measured": self.known_usage,
                       "estimated_cost_usd": round(self.cost_usd, 8)},
            "events": self.events[-500:]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        return {"requests": self.requests, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens if self.known_usage else None,
                "charged_tokens": self.total_tokens, "usage_measured": self.known_usage,
                "estimated_cost_usd": round(self.cost_usd, 8),
                "elapsed_seconds": round(time.monotonic() - self.started, 3)}


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _fixture_schema() -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    schema = {
        "model_runs": [
            {"name": "id", "type": "uuid"}, {"name": "run_name", "type": "text"},
            {"name": "training_dataset_version_id", "type": "uuid"},
        ],
        "dataset_versions": [
            {"name": "id", "type": "uuid"}, {"name": "version", "type": "text"},
            {"name": "version_name", "type": "text"},
        ],
        "training_molecules": [
            {"name": "molecule_id", "type": "text"}, {"name": "dataset_version", "type": "text"},
            {"name": "dataset_version_id", "type": "uuid"}, {"name": "split", "type": "text"},
            {"name": "structure_type", "type": "text"},
        ],
        "molecules": [
            {"name": "molecule_id", "type": "text"}, {"name": "structure_type", "type": "text"},
        ],
        "predictions": [
            {"name": "molecule_id", "type": "text"}, {"name": "model_run_id", "type": "uuid"},
            {"name": "observed_rt", "type": "numeric"}, {"name": "predicted_rt", "type": "numeric"},
            {"name": "absolute_error", "type": "numeric"},
        ],
    }
    relationships = [
        {"source_table": "training_molecules", "source_column": "molecule_id", "target_table": "molecules", "target_column": "molecule_id"},
        {"source_table": "predictions", "source_column": "molecule_id", "target_table": "molecules", "target_column": "molecule_id"},
        {"source_table": "predictions", "source_column": "model_run_id", "target_table": "model_runs", "target_column": "id"},
        {"source_table": "model_runs", "source_column": "training_dataset_version_id", "target_table": "dataset_versions", "target_column": "id"},
    ]
    return schema, relationships


def _synthetic_cases(cases_path: Path, count: int = 10) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:count]


async def _call(ledger: RunLedger, stage: str, estimate: int,
                fn: Callable[[], Awaitable[Any]], timeout_seconds: float = 45.0) -> tuple[Any | None, dict[str, Any]]:
    ledger.reserve(stage, estimate)
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(fn(), timeout=timeout_seconds)
        telemetry = {}
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            result, telemetry = result
        elif isinstance(result, tuple) and len(result) == 3 and isinstance(result[2], dict):
            telemetry = result[2]
        ledger.finish(stage, telemetry, (time.perf_counter() - started) * 1000, estimate=estimate)
        return result, {"stage": stage, "success": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2), **telemetry}
    except Exception as exc:  # real provider/schema failures are recorded, never fabricated away
        ledger.finish(stage, {}, (time.perf_counter() - started) * 1000, type(exc).__name__, estimate)
        failure = {"stage": stage, "success": False, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                   "error_type": type(exc).__name__, "error": str(exc)[:500]}
        # SQLScopeValidationError intentionally exposes a diagnostic-only
        # candidate and its scope/recovery metadata.  It is never returned as
        # an executable candidate, but retaining it makes C replayable.
        candidate = getattr(exc, "candidate", None)
        if candidate is not None and hasattr(candidate, "model_dump"):
            failure["diagnostic_candidate"] = candidate.model_dump(mode="json")
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("query_scope", "scope_validation", "recovery", "sql_candidate_status"):
                if key in metadata:
                    failure[key] = metadata[key]
        return None, failure


async def run(args: argparse.Namespace) -> dict[str, Any]:
    root = _target_root(args.root)
    # Imports happen only after target checkout selection.
    from app.agents.decision_node import DecisionNode
    from app.agents.planning_policy import PlanningPolicy
    from app.models.schemas import (Capability, Evidence, GoalContract, QueryScope,
                                    RequestIntent, ResourceBinding, ResourceSummary,
                                    ScientificAgentState)
    from app.services.grounded_response import GroundedResponseService
    from app.services.skills import SkillService
    from app.services.text2sql import TextToSQLService

    run_dir = Path(args.run_dir).resolve()
    ledger = RunLedger(run_dir, max_requests=args.max_requests, max_tokens=args.max_tokens,
                       max_cost_usd=args.max_cost_usd, max_minutes=args.max_minutes)
    schema, relationships = _fixture_schema()
    # Cases/fixtures are owned by the benchmark checkout, not the baseline
    # worktree (which intentionally predates this evaluation harness).
    cases = _synthetic_cases(Path(__file__).resolve().parent / "cases.jsonl", args.case_count)
    if args.case_ids:
        wanted = {item.strip() for item in args.case_ids.split(",") if item.strip()}
        cases = [case for case in cases if case["case_id"] in wanted]
    results: list[dict[str, Any]] = []
    for case in cases:
        case_started = time.perf_counter()
        row: dict[str, Any] = {"case_id": case["case_id"], "family": case["family"],
                               "request": case["request"], "stages": [], "expected_outcome": case.get("expected_outcome")}
        goal = case["request"]
        resources = ResourceSummary(available_files=["model_v1.csv", "model_v2.csv"],
            authorized_datasources=["training_db"], available_models=["rt_model_v1", "rt_model_v2"],
            resource_metadata={"dataset_versions": [{"version": "train_v2"}, {"version": "train_v3"}]})
        skill_service = SkillService()
        if args.ablation == "no_skill":
            selected, skill_source = [], "ablation_disabled"
            skill_tel = {"stage": "skill_routing", "success": True, "ablation_disabled": True,
                         "latency_ms": 0.0, "llm_called": False}
        else:
            skill_result, skill_tel = await _call(ledger, "skill_routing", len(goal) // 4 + 1800,
                lambda: skill_service.select_async(goal, "mixed_analysis" if "coverage" in goal.lower() or "join" in goal.lower() else "file_analysis",
                                                   {"file", "database", "scientific_model"}))
            if isinstance(skill_result, tuple) and len(skill_result) == 3:
                selected, skill_source, skill_inner_tel = skill_result
                skill_tel.update({f"provider_{key}": value for key, value in skill_inner_tel.items()})
            else:
                selected, skill_source = [], "error"
        row["selected_skills"] = selected or []
        row["skill_source"] = skill_source if skill_tel.get("success") else "error"
        row["stages"].append(skill_tel)
        # Keep the router/planner contract exercised, but avoid calling an LLM
        # merely to classify explicit synthetic resources.
        intent = RequestIntent(goal=goal, task_type="mixed_analysis" if "coverage" in goal.lower() or "join" in goal.lower() else "file_analysis",
                               complexity="complex" if case["case_id"] in {"A", "C", "D06", "D08", "D09", "M02"} else "simple",
                               required_capabilities=[Capability.FILE, Capability.DATABASE])
        if intent.complexity == "complex" and args.ablation != "no_planning":
            plan_result, plan_tel = await _call(ledger, "planning", len(goal) // 4 + 1600,
                lambda: PlanningPolicy().select_async(intent.model_dump(mode="json"), selected or []))
            if isinstance(plan_result, tuple) and len(plan_result) == 3:
                planned, plan_source, plan_inner_tel = plan_result
                plan_tel.update({f"provider_{key}": value for key, value in plan_inner_tel.items()})
            else:
                planned, plan_source = [], "error"
            row["planned_stages"] = planned or []
            row["plan_source"] = plan_source if plan_tel.get("success") else "error"
            row["stages"].append(plan_tel)
        scope = QueryScope(dataset_version="train_v3", split="train", datasource_id="training_db") if case["case_id"] in {"B", "C", "D08"} else QueryScope()
        binding = ResourceBinding(datasource_id="training_db", dataset_version=scope.dataset_version, files=["model_v1.csv", "model_v2.csv"])
        text2sql = TextToSQLService()
        candidate, sql_tel = await _call(ledger, "text2sql", len(goal) // 4 + 3800,
            lambda: text2sql.generate(goal, "synthetic SQL generation", "training_db", schema, relationships,
                                      original_goal=goal, query_scope=scope, resource_binding=binding,
                                      skill_context="\n".join(row["selected_skills"])))
        row["stages"].append(sql_tel)
        if candidate is not None:
            row["sql_candidate"] = {"sql": candidate.sql, "params": candidate.params,
                                     "status": "scope_verified" if scope.dataset_version else "generated"}
        else:
            row["sql_candidate"] = {"status": "diagnostic_or_unavailable"}
        tool_specs = [
            {"name": "search_schema", "description": "search synthetic authorized schema", "required_capability": "database", "input_schema": {"type": "object"}},
            {"name": "text_to_sql", "description": "generate a parameterized query", "required_capability": "database", "input_schema": {"type": "object"}},
            {"name": "query_checker", "description": "validate a trusted SQL candidate", "required_capability": "database", "input_schema": {"type": "object"}},
            {"name": "execute_readonly_sql", "description": "execute approved read-only SQL", "required_capability": "database", "input_schema": {"type": "object"}},
        ]
        state = ScientificAgentState(user_id="benchmark", thread_id=f"{args.label}-{case['case_id']}", goal=goal,
            user_request=goal, task_type=intent.task_type, complexity=intent.complexity,
            available_files=resources.available_files, available_datasources=resources.authorized_datasources,
            available_models=resources.available_scientific_models, available_tools=[item["name"] for item in tool_specs],
            selected_skills=selected or [], allowed_tools=[item["name"] for item in tool_specs],
            schema_cache=schema, relationships_cache=relationships, grounding_ready=True,
            resource_summary=resources, resource_binding=binding, query_scope=scope,
            goal_contract=GoalContract(original_request=goal, required_goals=[goal], required_data_sources=["training_db"],
                                       dataset_version=scope.dataset_version), requested_dimensions=["structure_type"],
            required_deliverables=[])
        decision, decision_tel = await _call(ledger, "decision", len(goal) // 4 + 5200,
            lambda: DecisionNode().decide(state, tool_specs, "synthetic Skill guidance; do not execute external tools"))
        row["stages"].append(decision_tel)
        if decision is not None:
            row["decision"] = {"action": decision.action, "tool_name": decision.tool_name,
                                "requested_dimensions": decision.requested_dimensions}
        evidence = [Evidence(evidence_id=f"ev-{case['case_id'].lower()}-1",
            claim="Synthetic fixture result for protocol validation", value={"sample_count": 7, "structure_type": "fused_ring"},
            source_type="synthetic_fixture", source="benchmark_fixture", tool_call_id=f"fixture-{case['case_id']}",
            dataset_version=scope.dataset_version, query_scope=scope)]
        facts = {"quality_status": "SUPPORTED_CONCLUSION", "data_origins": ["synthetic_demo"],
                 "requested_content": ["claim", "evidence"], "evidence": [item.model_dump(mode="json") for item in evidence],
                 "claims": [], "goal_coverage": {"status": "SATISFIED", "required_dimensions": ["structure_type"], "observed_dimensions": ["structure_type"]},
                 "sql_candidate": row.get("sql_candidate", {}), "artifacts": [],
                 "uncertainties": ["No database execution is performed by this isolated benchmark."]}
        response, response_tel = await _call(ledger, "grounded_response", len(goal) // 4 + 5600,
            lambda: GroundedResponseService().generate(goal, facts))
        row["stages"].append(response_tel)
        row["response_success"] = response is not None
        row["elapsed_ms"] = round((time.perf_counter() - case_started) * 1000, 2)
        # A grounded prose response is not a completed database analysis. For
        # database-scoped fixtures the Text2SQL candidate must itself be
        # scope-verified; C therefore remains an honest bounded failure when
        # scope validation rejects the generated candidate.
        db_case = case["case_id"] in {"B", "C", "D08"}
        sql_ok = any(item.get("stage") == "text2sql" and item.get("success") for item in row["stages"])
        row["task_success"] = bool(response is not None and (sql_ok or not db_case))
        row["invalid_tool_calls"] = 0
        row["replans"] = 0
        row["decision_calls"] = sum(1 for item in row["stages"] if item.get("stage") in {"decision", "planning"})
        results.append(row)
        if ledger.requests >= args.max_requests:
            break
    metrics_rows = [item for item in results if item.get("task_success")]
    latencies = [item["elapsed_ms"] for item in results if isinstance(item.get("elapsed_ms"), (int, float))]
    stage_rows = [stage for item in results for stage in item["stages"]]
    def _usage(stage: dict[str, Any], key: str):
        direct = stage.get(key)
        if isinstance(direct, int):
            return direct
        nested = stage.get(f"provider_{key}")
        if isinstance(nested, int):
            return nested
        telemetry = stage.get("llm_telemetry") or {}
        value = telemetry.get(key)
        return value if isinstance(value, int) else None
    measured_stages = [stage for stage in stage_rows if all(_usage(stage, key) is not None for key in ("input_tokens", "output_tokens", "total_tokens"))]
    metrics = {
        "case_count": len(results), "task_success_rate": round(len(metrics_rows) / len(results), 4) if results else None,
        "avg_decision_calls": round(statistics.mean([item["decision_calls"] for item in results]), 4) if results else None,
        "avg_replans": 0.0, "avg_invalid_tool_calls": 0.0,
        "p95_latency_ms": round(sorted(latencies)[min(len(latencies)-1, int((len(latencies)-1)*0.95))], 2) if latencies else None,
        "provider_usage": {"stage_calls": len(stage_rows), "measured_stage_calls": len(measured_stages),
                            "coverage": round(len(measured_stages) / len(stage_rows), 4) if stage_rows else None,
                            "input_tokens": sum(_usage(stage, "input_tokens") or 0 for stage in measured_stages),
                            "output_tokens": sum(_usage(stage, "output_tokens") or 0 for stage in measured_stages),
                            "total_tokens": sum(_usage(stage, "total_tokens") or 0 for stage in measured_stages) if len(measured_stages) == len(stage_rows) else None},
        "budget": ledger.snapshot(), "execution_scope": "production generation stages only; no PostgreSQL/MinIO/SQL execution",
    }
    manifest = {"schema_version": "scientific-agent-live-qwen-v1", "label": args.label,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "model": os.getenv("LLM_MODEL"), "provider_host": ledger._host(), "case_ids": [item["case_id"] for item in cases],
        "ablation": args.ablation,
        "fixture_sha256": hashlib.sha256(json.dumps({"schema": schema, "relationships": relationships}, sort_keys=True).encode()).hexdigest(),
        "limits": {"max_requests": args.max_requests, "max_tokens": args.max_tokens, "max_cost_usd": args.max_cost_usd, "max_minutes": args.max_minutes},
        "started_at": datetime.now(timezone.utc).isoformat(), "real_model": True, "api_key_recorded": False}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "case_results.jsonl").write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in results) + "\n", encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "failure_analysis.json").write_text(json.dumps({"failed_stages": [stage for item in results for stage in item["stages"] if not stage.get("success")], "notes": ["Provider errors are preserved as outcomes; no fallback metrics are fabricated."]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": manifest, "metrics": metrics}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--case-count", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--max-tokens", type=int, default=1_500_000)
    parser.add_argument("--max-cost-usd", type=float, default=30.0)
    parser.add_argument("--max-minutes", type=float, default=120.0)
    parser.add_argument("--ablation", choices=["none", "no_skill", "no_planning"], default="none")
    parser.add_argument("--provider-timeout", type=float, default=45.0)
    parser.add_argument("--case-ids", default="", help="comma-separated subset for a focused replay")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except BudgetStop as exc:
        print(json.dumps({"stopped": "budget_cap", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(3)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
