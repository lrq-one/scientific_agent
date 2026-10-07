"""Controlled invalid-column observation -> one real Qwen SQL repair -> read-only execution."""

from __future__ import annotations

import asyncio
import json
import uuid

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import SQLCandidate
from app.services.llm_config import llm_settings


class FaultOnceGenerator:
    def __init__(self, real_generator):
        self.real_generator = real_generator
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SQLCandidate(
                sql=("SELECT bogus, count(*) AS sample_count FROM training_molecules "
                     "WHERE dataset_version = %(dataset_version)s GROUP BY bogus"),
                params={"dataset_version": kwargs["dataset_version"]},
                reason="controlled schema-mismatch fault injection",
            ), {"generator": "controlled_fault_injection"}
        return await self.real_generator.generate(**kwargs)


async def main() -> None:
    settings = llm_settings()
    if not settings.configured or settings.model != "qwen3.7-flash":
        raise RuntimeError("qwen3.7-flash configuration unavailable; key is never printed")
    agent = ScientificAgent()
    fault = FaultOnceGenerator(agent.text2sql)
    agent.text2sql = fault
    events = [item async for item in agent.stream(
        "统计 training_db 中 train_v3 各结构类型训练覆盖。",
        f"phase45-batch3-{uuid.uuid4()}", f"phase45-batch3-{uuid.uuid4()}",
        "training_db", dataset_version="train_v3", skip_hitl=True,
    )]
    revised = [item for item in events if item.event == "PLAN_REVISED"]
    sql_outputs = [item.data["result"] for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "text_to_sql"]
    evidence = [item for item in events if item.event == "EVIDENCE_ADDED"]
    final = next((item for item in events if item.event == "FINAL_ANSWER"), None)
    result = {
        "model": settings.model,
        "real_llm": bool(sql_outputs and sql_outputs[-1].get("metadata", {}).get("generator") == "llm_structured_output"),
        "fallback": any(output.get("metadata", {}).get("generator") == "deterministic_fixture_fallback" for output in sql_outputs),
        "replan_count": len(revised),
        "repair_reason": revised[0].data.get("replan_reason") if revised else None,
        "sql_generators": [output.get("metadata", {}).get("generator") for output in sql_outputs],
        "evidence_count": len(evidence),
        "quality_status": final.data["state"]["quality_status"] if final else None,
        "events": [item.event for item in events],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not (result["real_llm"] and not result["fallback"] and result["replan_count"] == 1
            and result["evidence_count"] > 0 and result["quality_status"] == "SUPPORTED_CONCLUSION"):
        raise RuntimeError("controlled real-Qwen replan smoke did not complete")


if __name__ == "__main__":
    asyncio.run(main())
