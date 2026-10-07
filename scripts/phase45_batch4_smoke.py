"""One read-only live Skill smoke. Prints no credential or unrestricted response payload."""

from __future__ import annotations

import asyncio
import json

from app.agents.scientific_agent import ScientificAgent
from app.services.llm_config import llm_settings


async def main() -> None:
    settings = llm_settings()
    events = [event async for event in ScientificAgent().stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型与训练覆盖",
        "phase45-smoke", "phase45-batch4-skill",
    )]
    final = events[-1]
    state = final.data.get("state", {})
    selected = next((item for item in events if item.event == "TOOL_CANDIDATES"), None)
    sql = next((item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "execute_readonly_sql"), None)
    payload = {
        "model_configured": settings.model,
        "llm_tool_selection": selected.data.get("selection_source") if selected else None,
        "selected_skills": state.get("selected_skills", []),
        "tool_calls": [call.get("tool") for call in state.get("tool_calls", [])],
        "sql_rows": len(sql.data["result"].get("data", [])) if sql else None,
        "sql_read_only": sql.data["result"].get("metadata", {}).get("read_only") if sql else None,
        "evidence_count": len(state.get("evidence", [])),
        "quality_status": state.get("quality_status"),
        "llm_fallback": selected.data.get("selection_source") == "deterministic_fallback" if selected else None,
        "llm_telemetry": selected.data.get("llm_telemetry") if selected else None,
    }
    print(json.dumps(payload, ensure_ascii=False))
    if final.event != "FINAL_ANSWER" or payload["quality_status"] != "SUPPORTED_CONCLUSION":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
