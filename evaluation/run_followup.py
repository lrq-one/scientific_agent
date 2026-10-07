from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from langchain_openai import ChatOpenAI

from app.services.followup import (
    REUSE_TYPES,
    ConversationContextResolver,
    build_provenance,
    provenance_answer,
    workflow_query,
)
from evaluation.metrics import percentile
from evaluation.run_e2e import UsageCallback, delta


ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "followup" / "cases.jsonl"
FROZEN_PATH = ROOT / "frozen_config.json"
MODEL = "qwen3.7-flash"
MAX_TOKENS = 2048


def persisted_context() -> dict[str, Any]:
    sql = (
        "SELECT m.structure_type, count(*) AS sample_count, "
        "avg(p.absolute_error) AS avg_absolute_error "
        "FROM molecules m JOIN predictions p ON p.molecule_id = m.id "
        "WHERE p.dataset_version = %(dataset_version)s GROUP BY m.structure_type"
    )
    rows = [
        {"structure_type": "fused_ring", "sample_count": 2, "avg_absolute_error": 0.42},
        {"structure_type": "linear", "sample_count": 8, "avg_absolute_error": 0.19},
    ]
    tools = ["schema_retrieval", "schema_relationships", "text_to_sql", "query_checker"]
    events = [
        {
            "event_type": "TOOL_FINISHED",
            "payload_json": {
                "tool": tool,
                "result": {"success": True, "source": "training_db", "data": {}},
            },
        }
        for tool in tools
    ]
    events += [
        {
            "event_type": "TOOL_FINISHED",
            "payload_json": {
                "tool": "text_to_sql",
                "result": {
                    "success": True,
                    "source": "training_db",
                    "data": {"sql": sql, "params": {"dataset_version": "train_v3"}},
                },
            },
        },
        {
            "event_type": "TOOL_FINISHED",
            "payload_json": {
                "tool": "execute_readonly_sql",
                "result": {
                    "success": True,
                    "source": "training_db",
                    "data": rows,
                    "metadata": {"sql": sql, "params": {"dataset_version": "train_v3"}},
                },
            },
        },
    ]
    return {
        "task": {
            "id": "persisted-task-train-v3",
            "conversation_id": "persisted-conversation",
            "thread_id": "persisted-thread",
            "status": "completed",
            "intent_json": {"task_type": "database_analysis"},
            "selected_skills_json": ["training_coverage_analysis", "mass_spec_error_analysis"],
        },
        "events": events,
        "evidence": [
            {
                "id": "evidence-coverage-001",
                "claim": "训练覆盖与预测误差统计",
                "value_json": rows,
                "source_type": "database",
                "source": "training_db",
                "tool_call_id": "tool-5",
                "dataset_version": "train_v3",
                "model_version": "model_v2",
            },
            {
                "id": "evidence-fused-002",
                "claim": "训练集中 fused_ring 覆盖数",
                "value_json": 2,
                "source_type": "database",
                "source": "training_db",
                "tool_call_id": "tool-5",
                "dataset_version": "train_v3",
                "model_version": "model_v2",
            },
        ],
        "artifacts": [],
        "messages": [
            {"role": "user", "content": "统计 training_db 中 train_v3 不同结构类型覆盖。"}
        ],
        "assistant_message": {
            "content": "train_v3 中 fused_ring 有 2 条，平均绝对误差 0.42；这是相关性证据，不证明因果。"
        },
    }


def load_cases(split: str) -> list[dict[str, Any]]:
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    if digest != frozen["splits"]["followup"]:
        raise RuntimeError(f"follow-up dataset hash mismatch: {digest}")
    rows = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row["split"] == split]


async def collect(stream) -> list[dict[str, Any]]:
    events = []
    async for item in stream:
        events.append({"event": item.event, "message": item.message, "data": item.data})
    return events


def provenance_scores(answer: str, provenance, required: bool) -> tuple[float, float]:
    if not required:
        return 1.0, 1.0
    if not provenance.evidence:
        return 0.0, 0.0
    checks = [
        provenance.datasource == "training_db" and "training_db" in answer,
        provenance.dataset_version == "train_v3" and "train_v3" in answer,
        bool(provenance.sql_candidate) and str(provenance.sql_candidate.get("sql", "")) in answer,
        provenance.sql_raw_result is not None and "fused_ring" in answer and "0.42" in answer,
        all(str(item["id"]) in answer for item in provenance.evidence),
    ]
    citations = [str(item["id"]) in answer for item in provenance.evidence]
    return mean(checks), mean(citations) if citations else 0.0


async def run_case(agent, resolver, usage, case: dict[str, Any], method: str) -> dict[str, Any]:
    context = persisted_context()
    provenance = build_provenance(context)
    before = usage.snapshot()
    started = perf_counter()
    events: list[dict[str, Any]] = []
    workflow_invoked = False
    error = None
    if method == "baseline":
        predicted = "NEW_TASK"
        answer = ""
        workflow_invoked = True
    else:
        decision = await resolver.resolve_async(case["query"], context)
        predicted = decision.follow_up_type
        answer = provenance_answer(provenance) if predicted in REUSE_TYPES else ""
        workflow_invoked = predicted not in REUSE_TYPES
    if workflow_invoked:
        agent_query = case["query"]
        match = re.search(r"train[_-]?v\d+", case["query"], flags=re.I)
        dataset_version = match.group(0).replace("-", "_") if match else "train_v3"
        if method == "optimized":
            agent_query, restored_version = workflow_query(
                case["query"], predicted, context
            )
            dataset_version = restored_version or dataset_version
        try:
            events = await collect(
                agent.stream(
                    agent_query,
                    "followup-eval",
                    f"followup-{method}-{case['case_id']}",
                    datasource_id="training_db",
                    dataset_version=dataset_version,
                    skip_hitl=True,
                )
            )
            finals = [event for event in events if event["event"] == "FINAL_ANSWER"]
            answer = finals[-1]["data"].get("answer", "") if finals else ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    elapsed = (perf_counter() - started) * 1000
    used = delta(before, usage.snapshot())
    tool_calls = sum(event["event"] == "TOOL_FINISHED" for event in events)
    provenance_accuracy, citation_completeness = provenance_scores(
        answer, provenance if method == "optimized" else build_provenance(None), case["requires_provenance"]
    )
    return {
        "case_id": case["case_id"],
        "split": case["split"],
        "method": method,
        "query": case["query"],
        "gold_follow_up_type": case["gold_follow_up_type"],
        "predicted_follow_up_type": predicted,
        "routing_correct": predicted == case["gold_follow_up_type"],
        "requires_provenance": case["requires_provenance"],
        "workflow_invoked": workflow_invoked,
        "tool_calls": tool_calls,
        "unnecessary_tool_call": bool(case["requires_provenance"] and tool_calls > 0),
        "provenance_accuracy": provenance_accuracy,
        "citation_completeness": citation_completeness,
        "input_tokens": used["input_tokens"],
        "output_tokens": used["output_tokens"],
        "total_tokens": used["total_tokens"],
        "api_calls": used["api_calls"],
        "latency_ms": round(elapsed, 3),
        "answer": answer,
        "error": error,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    provenance_rows = [row for row in rows if row["requires_provenance"]]
    return {
        "cases": len(rows),
        "follow_up_routing_accuracy": mean(row["routing_correct"] for row in rows),
        "evidence_provenance_accuracy": mean(row["provenance_accuracy"] for row in provenance_rows),
        "evidence_citation_completeness": mean(row["citation_completeness"] for row in provenance_rows),
        "unnecessary_tool_call_rate": mean(row["unnecessary_tool_call"] for row in provenance_rows),
        "workflow_invocation_rate": mean(row["workflow_invoked"] for row in rows),
        "total_tool_calls": sum(row["tool_calls"] for row in rows),
        "mean_tool_calls": mean(row["tool_calls"] for row in rows),
        "tokens_per_follow_up": mean(row["total_tokens"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "api_calls": sum(row["api_calls"] for row in rows),
        "mean_latency_ms": mean(row["latency_ms"] for row in rows),
        "p95_follow_up_latency_ms": percentile([row["latency_ms"] for row in rows], 0.95),
        "error_rate": mean(row["error"] is not None for row in rows),
    }


async def main_async(split: str, method: str) -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not api_base or not api_key or model != MODEL:
        raise RuntimeError("qwen3.7-flash real-LLM environment is not configured")
    usage = UsageCallback()
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=api_base,
        temperature=0,
        max_tokens=MAX_TOKENS,
        max_retries=0,
        extra_body={"enable_thinking": False},
        callbacks=[usage],
    )
    from app.agents.scientific_agent import ScientificAgent

    agent = ScientificAgent()
    agent.router.llm = llm
    agent.tool_registry.llm = llm
    agent.text2sql.llm = llm
    agent.deep_runtime._model = lambda: llm
    resolver = ConversationContextResolver(llm=llm)
    rows = []
    cases = load_cases(split)
    for index, case in enumerate(cases, 1):
        row = await run_case(agent, resolver, usage, case, method)
        rows.append(row)
        print(
            f"{index}/{len(cases)} {case['case_id']} gold={case['gold_follow_up_type']} "
            f"pred={row['predicted_follow_up_type']} tools={row['tool_calls']} tokens={row['total_tokens']}",
            flush=True,
        )
    metrics = summarize(rows)
    suffix = "_goal_restore_v2" if method == "optimized" else ""
    run_dir = ROOT / "runs" / f"followup_{split}_{method}{suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "split": split,
                "method": method,
                "implementation_version": "goal_restore_v2" if method == "optimized" else "baseline",
                "model": model,
                "enable_thinking": False,
                "max_tokens": MAX_TOKENS,
                "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "per_case.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    parser.add_argument("--method", choices=["baseline", "optimized"], required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args.split, args.method))


if __name__ == "__main__":
    main()
