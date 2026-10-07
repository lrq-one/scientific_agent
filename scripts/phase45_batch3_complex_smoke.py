"""Real complex DB planning graph with controlled SQL schema fault and Qwen repair."""

from __future__ import annotations

import asyncio
import json
import uuid

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import Capability, RequestIntent
from app.services.llm_config import llm_settings
from scripts.phase45_batch3_smoke import FaultOnceGenerator


class ScopedComplexRouter:
    async def route_async(self, query, resources):
        return RequestIntent(
            goal=query, task_type="database_analysis", complexity="complex",
            required_capabilities=[Capability.DATABASE], need_planning=True,
            reason="controlled complex-plan smoke",
        )


async def main() -> None:
    settings = llm_settings()
    if not settings.configured or settings.model != "qwen3.7-flash":
        raise RuntimeError("qwen3.7-flash not configured")
    agent = ScientificAgent()
    agent.router = ScopedComplexRouter()
    agent.text2sql = FaultOnceGenerator(agent.text2sql)
    events = [item async for item in agent.stream(
        "比较 training_db 中 train_v3 各结构类型训练覆盖，并检查结构分组。",
        f"phase45-complex-{uuid.uuid4()}", f"phase45-complex-{uuid.uuid4()}",
        "training_db", dataset_version="train_v3", skip_hitl=True,
    )]
    final = next((item for item in events if item.event == "FINAL_ANSWER"), None)
    state = final.data.get("state") if final else {}
    plan = {step["step_id"]: step for step in state.get("plan", [])}
    sql_outputs = [item.data["result"] for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "text_to_sql"]
    result = {
        "model": settings.model,
        "real_llm": bool(sql_outputs and sql_outputs[-1].get("metadata", {}).get("generator") == "llm_structured_output"),
        "fallback": any(output.get("metadata", {}).get("generator") == "deterministic_fixture_fallback" for output in sql_outputs),
        "replan_count": state.get("replan_count"),
        "plan": {name: {"status": step["status"], "depends_on": step["depends_on"],
                        "evidence_count": len(step["evidence_ids"])} for name, step in plan.items()},
        "quality_status": state.get("quality_status"),
        "evidence_count": len(state.get("evidence", [])),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not (result["real_llm"] and not result["fallback"] and result["replan_count"] == 1
            and result["quality_status"] == "SUPPORTED_CONCLUSION"
            and plan.get("sql-execution", {}).get("status") == "completed"
            and plan["sql-execution"]["depends_on"] == ["sql-repair-1"]):
        raise RuntimeError("complex-plan real-Qwen smoke failed")


if __name__ == "__main__":
    asyncio.run(main())
