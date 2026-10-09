"""Infrastructure smoke uses only development records; never runs frozen tasks."""
import asyncio
import json
from evaluation.frozen.prepare import REPORT,dump
from evaluation.frozen.judge import judge_payload,grade
from evaluation.frozen.evaluate import score,summary,rows


async def main():
    source=REPORT/"development3"
    attempts=rows(REPORT/"development2/provider_attempts.jsonl")
    results=[]
    for name,query,kind in [("DEV-capability","你目前可以帮我做哪些科研数据分析？","no_tool"),
                           ("DEV-db","统计 training_db 中 eval_train_a 不同结构类型覆盖。","analysis")]:
        r=json.loads((source/(name+".json")).read_text(encoding="utf-8"))
        c={"case_id":name,"tags":["database"] if kind=="analysis" else ["simple"],"user_turns":[{"query":query}]}
        if kind=="analysis":c["user_turns"].append({"query":"SQL是什么？","expected_tool_reexecution":False})
        g={"case_id":name,"expected_outcome":kind,"expected_numeric_facts":[],"acceptable_skills":[],
           "required_capabilities":[],"clarification_required":kind=="analysis","required_evidence":kind=="analysis",
           "artifact_required":False,"security_expectation":"authorized_read_only","limitations":[],"acceptable_result_tolerance":.001}
        payload=judge_payload(c,g,r)
        j,usage=await grade(payload)
        dump(source/(name+"-judge.json"),{"input":payload,"output":j,"usage":usage})
        result=score(c,g,r,j,attempts);results.append(result)
    assert len(results)==2
    assert all(r["association_correct"] and r["isolation_correct"] for r in results)
    assert results[1]["followup"][0]["tools"]==0
    assert results[1]["llm_calls"]>0 and results[1]["total_tokens_observed"]>0
    assert all(r["unreported_usage_calls"]==0 for r in results)
    dump(source/"infrastructure_validation.json",{"results":results,"summary":summary(results),"development_only":True})
    print(json.dumps({"infrastructure_validated":True,"development_success":[r["task_success"] for r in results],"formal_cases_executed":0}))


if __name__=="__main__":asyncio.run(main())
