"""Evaluation-infra revision: restart ALL judging without changing frozen execution.

Original UI executions, product/prompts, cases, gold, fixtures and failed judging
are immutable. The only affected experiment is the independent scoring pass.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from time import perf_counter

from evaluation.frozen.prepare import REPORT as EXECUTION_REPORT,dump
from evaluation.frozen.freeze import verify,sha
from evaluation.frozen.judge import ConversationGrade,RUBRIC
from app.agents.decision_node import configured_llm
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector

REPORT=EXECUTION_REPORT/"grading_v1"
VERSION="phase4-v2.0-evalinfra.1"


async def bounded_judge(payload):
    from langchain_core.messages import SystemMessage,HumanMessage
    schema=ConversationGrade.model_json_schema()
    count=len(payload["case"]["user_turns"])
    schema["properties"]["turns"].update(minItems=count,maxItems=count)
    schema["properties"]["reason"]["maxLength"]=300
    schema["$defs"]["TurnGrade"]["properties"]["reason"]["maxLength"]=300
    collector=UsageCollector();started=perf_counter()
    result=await configured_llm(tokens=4000).with_structured_output(schema,method="json_schema").ainvoke(
        [SystemMessage(content=RUBRIC),HumanMessage(content=json.dumps(payload,ensure_ascii=False,default=str))],
        config={"callbacks":[collector]})
    validated=ConversationGrade.model_validate(result)
    if len(validated.turns)!=count:raise RuntimeError("Judge returned wrong number of user-turn grades")
    return validated.model_dump(mode="json"),{"actual_model":llm_settings().model,"max_output_tokens":4000,
        "latency_ms":round((perf_counter()-started)*1000,2),"fallback":False,**collector.snapshot()}


async def main():
    verify()
    if REPORT.exists():raise RuntimeError("Do not overwrite or selectively resume a grading revision")
    REPORT.mkdir()
    for name in ["gold","fixtures","runs","inspections"]:
        shutil.copytree(EXECUTION_REPORT/name,REPORT/name)
    for name in ["benchmark_manifest.jsonl","SPEC.md"]:
        shutil.copy2(EXECUTION_REPORT/name,REPORT/name)
    original=json.loads((EXECUTION_REPORT/"freeze_manifest.json").read_text(encoding="utf-8"))
    revised={**original,"benchmark_version":VERSION,"frozen_at":datetime.now(timezone.utc).isoformat(),
        "execution_benchmark_version":original["benchmark_version"],
        "execution_freeze_sha256":sha(EXECUTION_REPORT/"freeze_manifest.json"),
        "evaluation_infrastructure_revision":{
            "reason":"Independent judge LengthFinishReasonError at M02: unconstrained turns array and 2400-token ceiling",
            "changed":"Judge output array is bounded to exact user-turn count; public reasons capped at 300 characters; judge budget 4000 tokens",
            "unchanged":"ALL execution product/prompts/skill/tools/UI runner/cases/gold/fixtures/DB/model/runtime limits",
            "affected_experiment":"Independent judging/scoring pass on ALL 120 executions, restarted with no old grades reused",
            "ui_executions_repeated":False,"original_failure_preserved":"../judgements/full/M02.json",
            "source_file":"evaluation/regrade_frozen_v2.py","source_sha256":sha(Path(__file__)),
            "judge_rubric_sha256":hashlib.sha256(RUBRIC.encode()).hexdigest(),
            "judge_model":llm_settings().model,"judge_temperature":0,"judge_enable_thinking":False,"judge_max_output_tokens":4000}}
    dump(REPORT/"freeze_manifest.json",revised)
    from evaluation.frozen import evaluate,report
    evaluate.REPORT=REPORT
    evaluate.grade=bounded_judge
    await evaluate.main()
    verify()
    report.REPORT=REPORT
    report.main()
    readme=REPORT/"README.md"
    note="""\n## Evaluation infrastructure incident\n
All 120 UI executions completed under original phase4-v2.0 before grading. The initial independent
judge pass failed at FULL/M02 with LengthFinishReasonError (2400-token output ceiling and unbounded
turn array). Original failed grade and 38 partial grade records are preserved outside this revision.
No partial grades were reused: **all 120 grading cases were restarted** under phase4-v2.0-evalinfra.1.
Only judge schema bounds and judge output budget changed. Product code, execution Agent prompts,
UI harness, cases, gold, fixtures and execution model did not change; original execution freeze
hash verification passed before and after grading. **UI execution experiments were not repeated**:
they were unaffected by the post-execution independent-judge defect. This distinction is explicit,
not a claim that a new Agent version was tested. No Agent repair or test-driven product tuning occurred.
"""
    readme.write_text(readme.read_text(encoding="utf-8")+note,encoding="utf-8")
    dump(REPORT/"completion.json",{"execution_conversations":120,"full_cases":60,"grading_cases":120,
        "version":VERSION,"infrastructure_incident":True,"frozen_set_contamination":False,
        "product_tuned_after_freeze":False,"all_grades_restarted":True,"ui_executions_repeated":False})
    print("Revised complete grading pass and report finished; immutable UI/product results retained")


if __name__=="__main__":asyncio.run(main())
