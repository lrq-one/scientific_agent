"""Bounded real mixed-resource smoke; no credentials or unbounded data printed."""

from __future__ import annotations

import asyncio
import json
import uuid
from time import perf_counter

from app.agents.scientific_agent import ScientificAgent
from app.services.llm_config import llm_settings


async def main() -> None:
    started = perf_counter()
    first_progress_ms = None
    events = []
    async for item in ScientificAgent().stream(
        "比较 model_v1.csv 和 model_v2.csv 的 fused-ring 误差，并检查 training_db train_v3 训练覆盖。",
        "phase45-smoke", f"phase45-batch5-{uuid.uuid4().hex[:8]}", "training_db",
    ):
        if first_progress_ms is None:
            first_progress_ms = round((perf_counter() - started) * 1000, 2)
        events.append(item)
    agent_stream_ms = round((perf_counter() - started) * 1000, 2)
    final = events[-1]
    state = final.data.get("state", {})
    selector = next((item for item in events if item.event == "TOOL_CANDIDATES"), None)
    sql_candidate = next((item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "text_to_sql"), None)
    sql_execute = next((item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "execute_readonly_sql"), None)
    joined = next((item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "cross_resource_join"), None)
    intent_event = next((item for item in events if item.event == "INTENT_RESOLVED"), None)
    payload = {
        "model_configured": llm_settings().model,
        "agent_stream_ms": agent_stream_ms,
        "time_to_first_progress_ms": first_progress_ms,
        "last_event": final.event,
        "intent": state.get("task_type") or (intent_event.data.get("intent", {}).get("task_type") if intent_event else None),
        "selected_skills": state.get("selected_skills") or (intent_event.data.get("selected_skills") if intent_event else []),
        "event_names": [item.event for item in events],
        "wait_field": final.data.get("field") if final.event == "WAITING_FOR_USER" else None,
        "error_type": final.data.get("error_type") if final.event == "ERROR" else None,
        "tool_selection_source": selector.data.get("selection_source") if selector else None,
        "tool_selection_telemetry": selector.data.get("llm_telemetry") if selector else None,
        "sql_generator": sql_candidate.data["result"].get("metadata", {}).get("generator") if sql_candidate else None,
        "sql_llm_telemetry": sql_candidate.data["result"].get("metadata", {}).get("llm_telemetry") if sql_candidate else None,
        "sql_candidate": sql_candidate.data["result"].get("data") if sql_candidate else None,
        "sql_rows": len(sql_execute.data["result"].get("data", [])) if sql_execute else None,
        "join_rows": len(joined.data["result"].get("data", [])) if joined else None,
        "join_read_only": joined.data["result"].get("metadata", {}).get("read_only") if joined else None,
        "quality_status": state.get("quality_status"),
        "evidence_count": len(state.get("evidence", [])),
        "tool_calls": [call.get("tool") for call in state.get("tool_calls", [])],
    }
    print(json.dumps(payload, ensure_ascii=False))
    if (final.event != "FINAL_ANSWER" or payload["intent"] != "mixed_analysis"
            or payload["sql_generator"] != "llm_structured_output"
            or not payload["sql_rows"] or payload["join_rows"] != 0
            or payload["join_read_only"] is not True
            or payload["tool_selection_source"] != "llm_structured_selection"):
        raise SystemExit(1)
    if payload["quality_status"] != "INSUFFICIENT_EVIDENCE":
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
