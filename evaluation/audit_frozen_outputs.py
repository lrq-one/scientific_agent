"""Additional read-only audits of already frozen outputs; no execution or regrading."""
from collections import Counter
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import math
import subprocess

from evaluation.frozen.prepare import ROOT,REPORT as ORIGINAL,dump
from evaluation.frozen.evaluate import dist,wilson

REPORT=ORIGINAL/"grading_v1"


def read(path):return json.loads(path.read_text(encoding="utf-8"))


def cell_equal(value,expected):
    if expected is None:return value==""
    if isinstance(expected,bool):return value.lower()==str(expected).lower()
    if isinstance(expected,(int,float)):
        try:return math.isclose(float(value),expected,rel_tol=1e-9,abs_tol=1e-9)
        except ValueError:return False
    return value==str(expected)


def main():
    cases={c["case_id"]:c for c in map(json.loads,(ORIGINAL/"benchmark_manifest.jsonl").read_text(encoding="utf-8").splitlines())}
    outputs=[];follow=[];completion=[];cancel=[]
    for folder in (ORIGINAL/"runs").iterdir():
        if not folder.is_dir():continue
        providers=[json.loads(line) for line in (folder/"provider_attempts.jsonl").read_text(encoding="utf-8").splitlines()]
        for p in folder.glob("*.json"):
            if p.stem not in cases:continue
            r=read(p);c=cases[p.stem]
            for idx,t in enumerate(r["turns"]):
                if t.get("task",{}).get("status")=="cancelled":
                    cancel.append({"case_id":c["case_id"],"variant":folder.name,"task_id":t["task"]["id"],
                                  "cancelled_persisted":True,"reload_not_running":"取消执行" not in t.get("reload_button_text","")})
                exports=[e["payload_json"] for e in t.get("events",[]) if e["event_type"]=="TOOL_FINISHED" and
                         e["payload_json"].get("tool")=="save_result_table" and e["payload_json"].get("result",{}).get("success")]
                for d in t.get("downloads",[]):
                    file=folder/d["filename"];info={"case_id":c["case_id"],"variant":folder.name,"task_id":t["task"]["id"],"filename":d["filename"],"download_failure":d["failure"]}
                    if file.suffix.lower()==".csv" and file.exists():
                        with file.open(encoding="utf-8-sig",newline="") as f:actual=list(csv.DictReader(f))
                        matched=False
                        for export in exports:
                            expected=export.get("arguments",{}).get("rows",[])
                            if len(actual)==len(expected) and all(set(a)==set(e) and all(cell_equal(a[k],v) for k,v in e.items()) for a,e in zip(actual,expected)):
                                matched=True;break
                        info.update(actual_rows=len(actual),content_equals_successful_tool_rows=matched)
                    else:info["content_equals_successful_tool_rows"]=None
                    outputs.append(info)
            if "followup" in c["subsets"] and folder.name in {"full","stateless_followup"}:
                t=r["turns"][1] if len(r["turns"])>1 else None
                row={"case_id":c["case_id"],"variant":folder.name,"attempted":t is not None,
                     "reuse_only":c["user_turns"][1].get("expected_tool_reexecution") is False,
                     "completed":bool(t and t.get("task",{}).get("status")=="completed")}
                if t:
                    start=datetime.fromisoformat(t["started_at"]);end=start+timedelta(milliseconds=t["latency_ms"])
                    calls=[a for a in providers if a["phase"]=="provider_finished" and a.get("conversation_id")==r["conversation_id"] and start<=datetime.fromisoformat(a["started_at"])<=end]
                    row.update(latency_ms=t["system_latency_ms"],provider_calls=len(calls),
                        observed_tokens=sum((a.get("usage") or {}).get("total_tokens",0) for a in calls),
                        unreported_usage=sum(not a.get("usage") for a in calls),
                        tool_calls=sum(e["event_type"]=="TOOL_FINISHED" for e in t["events"]))
                else:row.update(latency_ms=None,provider_calls=None,observed_tokens=None,unreported_usage=None,tool_calls=None)
                follow.append(row)
    per_case=read(REPORT/"metrics/per_case.json")
    for variant in ["full","stateless_followup"]:
        subset=[r for r in follow if r["variant"]==variant]
        paired=[r for r in per_case if r["variant"]==variant and "followup" in cases[r["case_id"]]["subsets"]]
        completion.append({"variant":variant,"expected_followup_turns":len(subset),"attempted":sum(r["attempted"] for r in subset),
            "missing_turns_count_as_completion_failure":sum(not r["attempted"] for r in subset),
            "followup_completion_all_expected":wilson(sum(r["completed"] for r in subset),len(subset)),
            "context_correct_all_expected":wilson(sum(f["context"] for r in paired for f in r["followup"]),len(subset)),
            "structured_reuse_correct_all_expected":wilson(sum(f["reuse"] is True for r in paired for f in r["followup"]),
                 sum(c["user_turns"][1].get("expected_tool_reexecution") is False for c in cases.values() if "followup" in c["subsets"])),
            "latency_attempted_ms":dist([r["latency_ms"] for r in subset]),"tokens_attempted":dist([r["observed_tokens"] for r in subset]),
            "tools_attempted":dist([r["tool_calls"] for r in subset]),
            "reuse_only_latency_attempted_ms":dist([r["latency_ms"] for r in subset if r["reuse_only"]]),
            "reuse_only_tokens_attempted":dist([r["observed_tokens"] for r in subset if r["reuse_only"]]),
            "reuse_only_tools_attempted":dist([r["tool_calls"] for r in subset if r["reuse_only"]]),
            "missing_costs":"not measured; not imputed as zero"})
    dump(REPORT/"metrics/followup_turn_costs.json",follow)
    dump(REPORT/"metrics/followup_all_expected_summary.json",completion)
    dump(REPORT/"metrics/artifact_content_audit.json",outputs)
    dump(REPORT/"metrics/cancel_state_audit.json",cancel)
    git=Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe"
    status=subprocess.check_output([str(git),"status","--short"],cwd=ROOT,encoding="utf-8")
    (REPORT/"git_status_short.txt").write_text(status,encoding="utf-8")
    invalid=[r for r in outputs if r["content_equals_successful_tool_rows"] is False]
    dump(REPORT/"metrics/output_audit_summary.json",{"artifact_downloads":len(outputs),"csv_mismatches":invalid,
          "cancelled_tasks":len(cancel),"cancel_state_failures":[r for r in cancel if not r["reload_not_running"]]})
    if invalid:raise RuntimeError("Artifact content mismatch; do not claim those cases passed")
    print(json.dumps({"downloads_checked":len(outputs),"csv_mismatches":len(invalid),"followup":completion},ensure_ascii=False))


if __name__=="__main__":main()
