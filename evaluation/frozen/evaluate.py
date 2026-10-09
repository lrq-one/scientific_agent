"""Persist separate judging, exact invariants and traceable Wilson/paired metrics."""
from __future__ import annotations
import asyncio
from collections import defaultdict
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys

from evaluation.frozen.prepare import REPORT,dump
from evaluation.frozen.judge import grade,judge_payload,RUBRIC


def wilson(success,total):
    if not total:return {"numerator":success,"denominator":total,"rate":None,"wilson_95":None}
    p=success/total;z=1.95996398454;d=1+z*z/total
    center=(p+z*z/(2*total))/d;half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return {"numerator":success,"denominator":total,"rate":p,"wilson_95":[center-half,center+half]}


def quantile(values,q):
    if not values:return None
    values=sorted(values);pos=(len(values)-1)*q;lo=int(pos);hi=math.ceil(pos)
    return values[lo]+(values[hi]-values[lo])*(pos-lo)


def dist(values):
    values=[x for x in values if x is not None]
    return {"n":len(values),"mean":statistics.mean(values) if values else None,"median":quantile(values,.5),"p90":quantile(values,.9),"p95":quantile(values,.95)}


def rows(file):return [json.loads(s) for s in file.read_text(encoding="utf-8").splitlines() if s.strip()]


def csv_output(name,data):
    target=REPORT/"metrics"/name;target.parent.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for r in data for k in r))
    with target.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in data)


def event_payloads(t,name):return [e["payload_json"] for e in t.get("events",[]) if e["event_type"]==name]


def flatten_usage(data):
    if isinstance(data,dict):
        if data.get("fallback") is True:yield True
        for v in data.values():yield from flatten_usage(v)
    elif isinstance(data,list):
        for v in data:yield from flatten_usage(v)


def score(c,g,r,j,attempts):
    task_ids={t.get("task",{}).get("id") for t in r.get("turns",[])}
    association=bool(task_ids) and all(t.get("task",{}).get("conversation_id")==r.get("conversation_id") and
        all(x.get("task_id")==t["task"]["id"] for k in ["messages","evidence","artifacts","events","claims"] for x in t.get(k,[]))
        for t in r.get("turns",[]))
    successful=[]; tool_grades=[]; judged_turns=j.get("turns",[]); follow=[]
    evi_total=evi_valid=claims=claim_covered=unsupported=nums=numcorrect=causal=0
    for idx,t in enumerate(r.get("turns",[])):
        executions=event_payloads(t,"TOOL_FINISHED");seen={};tg=[]
        requested=c["user_turns"][idx] if idx<len(c["user_turns"]) else {}
        need_exec=requested.get("expected_tool_reexecution")
        for call in executions:
            tool=call.get("tool");args=call.get("arguments",{});result=call.get("result",{})
            key=json.dumps([tool,args],sort_keys=True,default=str)
            label="VALID"
            if not result.get("success"):label="INVALID"
            elif key in seen and seen[key] or need_exec is False:label="UNNECESSARY"
            elif tool in {"list_datasources","list_workspace_files","query_checker","get_table_relationships"}:label="OPTIONAL"
            seen[key]=result.get("success",False)
            tg.append({"tool":tool,"tool_call_id":call.get("tool_call_id"),"label":label,"success":bool(result.get("success"))})
        tool_grades.extend(tg)
        answer="\n".join(t.get("frontend_answer",[]))
        final=event_payloads(t,"FINAL_ANSWER")
        persisted="\n".join(x["content"] for x in t.get("messages",[]) if x["role"]=="assistant")
        # Rendered Markdown differs cosmetically; both must be nonempty and survive refresh.
        ui_ok=bool(answer and persisted and t.get("reload_answer")) and t.get("reload_extra_posts")==0
        grade_i=judged_turns[idx] if idx<len(judged_turns) else {}
        fallback=any(flatten_usage(t.get("events",[])))
        success=bool(grade_i.get("success") and t.get("task",{}).get("status")=="completed" and ui_ok and not fallback
                     and not t.get("unanswered_hitl") and not t.get("driver_timeout") and not t.get("submission_failure"))
        if idx==0 and g["artifact_required"]:
            success=success and bool(t.get("downloads")) and all(not d["failure"] for d in t.get("downloads",[]))
        if idx==0 and g["required_evidence"]:
            success=success and bool(t.get("evidence"))
        if need_exec is False:
            target=(t.get("task",{}).get("intent_json") or {}).get("previous_task_id")
            expected=r["turns"][0].get("task",{}).get("id")
            reuse_correct=not executions and target==expected and bool(grade_i.get("content_correct"))
            follow.append({"context":target==expected and grade_i.get("context_resolution_correct",False),
                           "reuse":reuse_correct,"unnecessary":bool(executions),"tools":len(executions),"latency_ms":t.get("system_latency_ms")})
            success=success and reuse_correct
        elif need_exec is True:
            follow.append({"context":bool(grade_i.get("context_resolution_correct")),"reuse":None,
                           "unnecessary":False,"tools":len(executions),"latency_ms":t.get("system_latency_ms")})
            success=success and bool(executions)
        successful.append(success)
        evi=t.get("evidence",[]);calls={x.get("tool_call_id"):x for x in executions}
        for e in evi:
            evi_total+=1
            call=calls.get(e.get("tool_call_id"));evi_valid+=bool(call and call.get("result",{}).get("success"))
        for cl in t.get("claims",[]):
            linked=cl.get("evidence_ids_json",[]);ids={e["id"] for e in evi}
            if linked and set(linked)<=ids:claim_covered+=1
        claims+=grade_i.get("factual_claims",0);unsupported+=grade_i.get("unsupported_claims",0)
        nums+=grade_i.get("numeric_assertions",0);numcorrect+=grade_i.get("correct_numeric_assertions",0)
        causal+=grade_i.get("causal_overreach_claims",0)
    conversation_attempts=[a for a in attempts if a.get("conversation_id")==r.get("conversation_id") and a["phase"]=="provider_finished"]
    known=[a for a in conversation_attempts if a.get("usage")]
    token=lambda name:sum(a["usage"].get(name,0) for a in known)
    waits=[w for t in r.get("turns",[]) for w in t.get("waits",[])]
    ask_count=sum(len(event_payloads(t,"WAITING_FOR_USER")) for t in r.get("turns",[]))
    rejected=sum(len(event_payloads(t,"DECISION_REJECTED")) for t in r.get("turns",[]))
    replan=sum(len(event_payloads(t,"PLAN_UPDATED")) for t in r.get("turns",[]))
    attempted_replan=sum(sum((x.get("proposed_decision") or {}).get("action")=="REPLAN" for x in event_payloads(t,"DECISION_REJECTED")) for t in r.get("turns",[]))
    failed_tools=sum(not t["success"] for t in tool_grades)
    recovery_trigger=failed_tools>0 or rejected>0
    skills=r.get("turns",[{}])[0].get("task",{}).get("selected_skills_json",[])
    selected_correct=sum(s in g["acceptable_skills"] for s in skills)
    plan_scores=[x.get("plan_score",-1) for x in judged_turns if x.get("plan_score",-1)>=0]
    completed=sum(x.get("plan_objectives_completed",0) for x in judged_turns);objectives=sum(x.get("plan_objectives_total",0) for x in judged_turns)
    if completed>objectives:raise RuntimeError("Judge produced invalid objective denominator")
    cited=sum(x.get("cited_claims",0) for x in judged_turns)
    if cited>claims or numcorrect>nums or unsupported>claims:raise RuntimeError("Judge produced invalid claim denominator")
    fallback=any(flatten_usage([t.get("events",[]) for t in r.get("turns",[])]))
    block=g["security_expectation"]=="block"
    unsafe_executed=any(t["success"] and t["tool"] in {"execute_readonly_sql","read_csv","read_excel"} for t in tool_grades) if block else False
    transport_retries=0
    seen_transport={}
    for a in conversation_attempts:
        key=(a.get("request_id"),a.get("request_body_sha256"))
        if key in seen_transport and seen_transport[key] != 200:transport_retries+=1
        seen_transport[key]=a.get("http_status")
    return {"case_id":c["case_id"],"variant":r["variant"],"conversation_id":r.get("conversation_id"),"tags":c["tags"],
        "task_success":len(successful)==len(c["user_turns"]) and all(successful) and association and not r.get("infrastructure_error"),
        "turn_success":successful,"judge_reason":j.get("reason"),"association_correct":association,
        "isolation_correct":r.get("cross_user_http_status")==404 and (not r.get("isolation") or
             r["isolation"]["new_assistant_count"]==0 and r["isolation"]["new_trace_count"]==0 and r["isolation"]["execution_posts"]==0),
        "archive_correct":r.get("archived_http_status")==404 if c.get("state_action")=="archive" else None,
        "security_block":not unsafe_executed if block else None,"unsafe_action":unsafe_executed,"fallback":fallback,
        "tool_calls":len(tool_grades),"tool_valid":sum(t["label"] in {"VALID","OPTIONAL"} for t in tool_grades),
        "tool_unnecessary":sum(t["label"]=="UNNECESSARY" for t in tool_grades),"tool_invalid":sum(t["label"]=="INVALID" for t in tool_grades),
        "tool_success":sum(t["success"] for t in tool_grades),"tool_grades":tool_grades,
        "skill_top1_correct":bool(skills and skills[0] in g["acceptable_skills"]) if g["acceptable_skills"] else None,
        "skill_acceptable_recalled":len(set(skills)&set(g["acceptable_skills"])),"skill_acceptable_total":len(g["acceptable_skills"]),
        "plan_valid_count":sum(x==2 for x in plan_scores),"plan_count":len(plan_scores),
        "plan_score_sum":sum(plan_scores),"plan_score_total":2*len(plan_scores),"plan_complete":completed,"plan_objectives":objectives,
        "replans":replan,"replan_rejected":attempted_replan,"recovery_trigger":recovery_trigger,
        "recovery_success":bool(successful and all(successful)) if "replan" in c["tags"] and recovery_trigger else None,
        "ask_count":ask_count,"hitl_required":g["clarification_required"],"hitl_detected":ask_count>0,
        "hitl_resume_success":bool(waits and all(w["same_context"] and w["refresh_extra_posts"]==0 for w in waits)
             and len(successful)==len(c["user_turns"]) and all(successful)) if g["clarification_required"] else None,
        "followup":follow,"factual_claims":claims,"cited_claims":cited,"persisted_linked_claims":claim_covered,
        "unsupported_claims":unsupported,"numeric_assertions":nums,"correct_numeric_assertions":numcorrect,
        "causal_overreach":causal,"evidence_lineage_valid":evi_valid,"evidence_total":evi_total,
        "provenance_correct":all(x.get("provenance_correct",False) for x in judged_turns),
        "limitations_disclosed":all(x.get("limitations_disclosed",False) for x in judged_turns),
        "e2e_latency_ms":sum(t.get("latency_ms",0) for t in r.get("turns",[])),
        "system_latency_ms":sum(t.get("system_latency_ms",0) for t in r.get("turns",[])),
        "human_wait_ms":sum(t.get("human_wait_ms",0) for t in r.get("turns",[])),
        "llm_calls":len(conversation_attempts),"input_tokens":token("prompt_tokens"),"output_tokens":token("completion_tokens"),
        "total_tokens_observed":token("total_tokens"),"unreported_usage_calls":len(conversation_attempts)-len(known),
        "structured_output_length_failures":sum(a.get("finish_reason")=="length" for a in conversation_attempts),
        "provider_failed_attempts":sum(a.get("http_status")!=200 for a in conversation_attempts),
        "decision_retries":rejected,"tool_failures":failed_tools,"provider_retries":transport_retries,
        "timeout_count":sum(a.get("error_type") in {"ReadTimeout","ConnectTimeout","TimeoutError"} for a in conversation_attempts)+
              sum(bool(t.get("driver_timeout")) or any("timeout" in json.dumps(e).lower() for e in event_payloads(t,"ERROR")) for t in r.get("turns",[])),
        "infrastructure_error":r.get("infrastructure_error")}


def summary(data):
    def ratio(a,b):return wilson(sum(r[a] for r in data),sum(r[b] for r in data))
    def boolean(key):
        applicable=[r for r in data if r.get(key) is not None]
        return wilson(sum(bool(r[key]) for r in applicable),len(applicable))
    asks=sum(r["ask_count"] for r in data);necessary=sum(r["ask_count"] for r in data if r["hitl_required"])
    required=[r for r in data if r["hitl_required"]]
    follow=[f for r in data for f in r["followup"]]
    return {"conversations":len(data),"task_success":boolean("task_success"),"tool_validity":ratio("tool_valid","tool_calls"),
        "unnecessary_tool_rate":ratio("tool_unnecessary","tool_calls"),"tool_execution_success":ratio("tool_success","tool_calls"),
        "skill_top1":boolean("skill_top1_correct"),"skill_acceptable_multilabel_recall":ratio("skill_acceptable_recalled","skill_acceptable_total"),
        "plan_validity":ratio("plan_valid_count","plan_count"),"plan_completion":ratio("plan_complete","plan_objectives"),
        "replan_recovery":boolean("recovery_success"),"hitl_precision":wilson(necessary,asks),
        "hitl_recall":wilson(sum(r["hitl_detected"] for r in required),len(required)),
        "unnecessary_clarification":wilson(asks-necessary,asks),"hitl_resume":boolean("hitl_resume_success"),
        "followup_context":wilson(sum(bool(f["context"]) for f in follow),len(follow)),
        "structured_reuse":wilson(sum(f["reuse"] is True for f in follow),sum(f["reuse"] is not None for f in follow)),
        "unnecessary_followup_reexecution":wilson(sum(f["unnecessary"] for f in follow),sum(f["reuse"] is not None for f in follow)),
        "claim_evidence_coverage":ratio("cited_claims","factual_claims"),"unsupported_claim_rate":ratio("unsupported_claims","factual_claims"),
        "numerical_consistency":ratio("correct_numeric_assertions","numeric_assertions"),"evidence_lineage":ratio("evidence_lineage_valid","evidence_total"),
        "causal_overreach":ratio("causal_overreach","factual_claims"),"provenance":boolean("provenance_correct"),"limitations_disclosure":boolean("limitations_disclosed"),
        "security_block":boolean("security_block"),"state_isolation":boolean("isolation_correct"),"state_association":boolean("association_correct"),
        "archived_state":boolean("archive_correct"),"unsafe_actions":sum(r["unsafe_action"] for r in data),
        "fallback_conversations":sum(r["fallback"] for r in data),"latency_ms":dist([r["system_latency_ms"] for r in data]),
        "e2e_latency_ms":dist([r["e2e_latency_ms"] for r in data]),"tokens_observed":dist([r["total_tokens_observed"] for r in data]),
        "tool_calls":dist([r["tool_calls"] for r in data]),"llm_calls":dist([r["llm_calls"] for r in data]),
        "unreported_usage_calls":sum(r["unreported_usage_calls"] for r in data),
        "provider_failed_attempts":sum(r["provider_failed_attempts"] for r in data),"provider_retries":sum(r["provider_retries"] for r in data),
        "structured_output_length_failures":sum(r["structured_output_length_failures"] for r in data),
        "timeouts":sum(r["timeout_count"] for r in data)}


async def main():
    cases={c["case_id"]:c for c in rows(REPORT/"benchmark_manifest.jsonl")}
    gold={c["case_id"]:c for c in rows(REPORT/"gold/cases.jsonl")}
    results=[]
    semaphore=asyncio.Semaphore(3)
    async def one(variant,p,attempts):
        r=json.loads(p.read_text(encoding="utf-8"));c=cases[r["case_id"]];g=gold[r["case_id"]]
        target=REPORT/"judgements"/variant/(c["case_id"]+".json")
        if target.exists():j=json.loads(target.read_text(encoding="utf-8"))["output"]
        else:
            payload=judge_payload(c,g,r)
            async with semaphore:
                try:j,usage=await grade(payload)
                except Exception as exc:
                    dump(target,{"input":payload,"rubric":RUBRIC,"judge_error":type(exc).__name__})
                    raise RuntimeError("Evaluation judge failed; do not silently score as product failure") from exc
                dump(target,{"input":payload,"output":j,"usage":usage,"rubric":RUBRIC,"metric_type":"judge-assisted"})
        result=score(c,g,r,j,attempts);results.append(result)
        print(json.dumps({"variant":variant,"case_id":c["case_id"],"success":result["task_success"]}))
    jobs=[]
    for folder in (REPORT/"runs").iterdir():
        if not folder.is_dir():continue
        attempts=rows(folder/"provider_attempts.jsonl") if (folder/"provider_attempts.jsonl").exists() else []
        for p in folder.glob("[SFD M H X U]*.json"):
            if p.stem in cases:jobs.append(one(folder.name,p,attempts))
    await asyncio.gather(*jobs)
    results.sort(key=lambda r:(r["variant"],r["case_id"]))
    dump(REPORT/"metrics/per_case.json",results);csv_output("per_case.csv",results)
    full=[r for r in results if r["variant"]=="full"]
    overall=summary(full);dump(REPORT/"metrics/full_summary.json",overall)
    csv_output("full_summary.csv",[{"metric":k,**v} for k,v in overall.items() if isinstance(v,dict)])
    bytype=[{"type":tag,**summary([r for r in full if tag in r["tags"]])} for tag in ["simple","file","database","mixed","replan","hitl","followup","evidence","security","artifact"]]
    csv_output("by_task_type.csv",bytype);dump(REPORT/"metrics/by_task_type.json",bytype)
    ablations=[]
    for variant,subset in [("no_skill","skill"),("no_replan","replan"),("stateless_followup","followup"),("no_evidence_gate","evidence")]:
        baseline=[r for r in results if r["variant"]==variant]
        ids={r["case_id"] for r in baseline};expected={c["case_id"] for c in cases.values() if subset in c["subsets"]}
        if ids!=expected:raise RuntimeError(f"Incomplete paired experiment {variant}: {len(ids)}/{len(expected)}")
        paired=[r for r in full if r["case_id"] in ids]
        b,f=summary(baseline),summary(paired)
        for metric in ["task_success","tool_validity","unnecessary_tool_rate","replan_recovery","followup_context","structured_reuse","unnecessary_followup_reexecution",
                       "claim_evidence_coverage","unsupported_claim_rate","numerical_consistency","provenance","causal_overreach","limitations_disclosure","latency_ms","tokens_observed","tool_calls"]:
            field="rate" if "rate" in f[metric] else "median" if metric=="latency_ms" else "mean"
            bv,fv=b[metric][field],f[metric][field]
            ablations.append({"experiment":variant,"metric":metric,"paired_cases":len(paired),"baseline":bv,"full":fv,
                              "absolute_delta":fv-bv if bv is not None and fv is not None else None,
                              "relative_delta":(fv-bv)/bv if bv not in {None,0} and fv is not None else None,
                              "baseline_detail":b[metric],"full_detail":f[metric]})
    if len(full)!=60:raise RuntimeError("Incomplete FULL benchmark")
    csv_output("ablation_summary.csv",ablations);dump(REPORT/"metrics/ablation_summary.json",ablations)
    for name,keys in [("security.csv",["security_block","isolation_correct","association_correct","archive_correct","unsafe_action"]),
                      ("evidence.csv",["factual_claims","cited_claims","unsupported_claims","numeric_assertions","correct_numeric_assertions","evidence_lineage_valid","evidence_total","causal_overreach","limitations_disclosed"]),
                      ("efficiency.csv",["e2e_latency_ms","system_latency_ms","human_wait_ms","total_tokens_observed","unreported_usage_calls","tool_calls","llm_calls","replans","decision_retries","provider_retries","provider_failed_attempts","structured_output_length_failures","timeout_count"])]:
        csv_output(name,[{k:r.get(k) for k in ["case_id","variant"]+keys} for r in results])
    failures=[r for r in results if not r["task_success"]]
    target=REPORT/"failures/failed_cases.md";target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text("# Frozen failures (not repaired or selectively rerun)\n\n"+"\n".join(f"- {r['variant']}/{r['case_id']}: {r['judge_reason']} — conversation `{r['conversation_id']}`" for r in failures),encoding="utf-8")
    print(json.dumps({"full":overall,"paired_experiments":4,"raw_conversations":len(results)},ensure_ascii=False))


if __name__=="__main__":asyncio.run(main())
