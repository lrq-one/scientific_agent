"""Freeze hashes and assert their immutability before each formal experiment."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess

from evaluation.frozen.prepare import REPORT,ROOT,dump


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    paths=[]
    for folder,pattern in [("app","*.py"),("skills","*"),("web/src","*"),("evaluation/frozen","*.py")]:
        paths.extend(p for p in (ROOT/folder).rglob(pattern) if p.is_file() and "__pycache__" not in p.parts)
    paths.extend([ROOT/"web/scripts/frozenBenchmark.mjs",ROOT/"web/package-lock.json",ROOT/"web/package.json",
                  ROOT/"scripts/run-evaluation.ps1",ROOT/"scripts/start-evaluation.ps1",ROOT/"scripts/run-frozen-experiments.ps1",ROOT/"docker/init.sql"])
    paths.extend(p for p in (ROOT/"web/dist").rglob("*") if p.is_file())
    paths.extend(p for folder in ["fixtures","gold"] for p in (REPORT/folder).rglob("*") if p.is_file())
    paths.append(REPORT/"benchmark_manifest.jsonl")
    return sorted(set(paths))


def snapshot_hash():
    import psycopg
    from psycopg.rows import dict_row
    from app.tools.registry import TOOL_SPECS
    tables=["molecules","training_molecules","datasets","dataset_versions","molecular_features","training_memberships",
            "experiments","model_versions","model_runs","retention_time_measurements","predictions","msms_spectra","annotations"]
    data={}
    with psycopg.connect("postgresql://scientific:scientific@127.0.0.1:55432/phase4_eval_v2",row_factory=dict_row) as c:
        for table in tables:
            rows=[{k:v for k,v in r.items() if k!="created_at"} for r in c.execute("SELECT * FROM "+table).fetchall()]
            data[table]=sorted(rows,key=lambda r:json.dumps(r,sort_keys=True,default=str))
    serial=json.dumps(data,sort_keys=True,default=str).encode()
    return {"version":"phase4-v2.0","database":"phase4_eval_v2","sha256":hashlib.sha256(serial).hexdigest(),
            "row_counts":{k:len(v) for k,v in data.items()},"tables":tables}


def freeze():
    if (REPORT/"freeze_manifest.json").exists():raise RuntimeError("Refuse to overwrite a freeze")
    from app.config import MAX_TOOL_CALLS,MAX_REPLANS,TASK_TIMEOUT
    from app.services.llm_config import llm_settings
    head=(ROOT/".git/HEAD").read_text().strip();branch=head.removeprefix("ref: refs/heads/")
    git=shutil.which("git") or str(Path(os.environ["LOCALAPPDATA"]).parents[1]/".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe")
    ref=ROOT/".git"/head.removeprefix("ref: ")
    revision=ref.read_text().strip() if ref.exists() else subprocess.check_output([git,"rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    status=subprocess.check_output([git,"status","--short"],cwd=ROOT,text=True,encoding="utf-8") if git else "not_measured: git executable unavailable; source tree hashes included"
    versions={name:importlib.metadata.version(name) for name in ["langchain-openai","openai","langgraph","sqlglot","httpx","psycopg"]}
    settings=llm_settings()
    if not settings.configured or settings.model!="qwen3.7-flash":raise RuntimeError("Required model is not configured")
    files={p.relative_to(ROOT).as_posix():sha(p) for p in source_files()}
    digest=lambda prefix:hashlib.sha256(json.dumps({k:v for k,v in files.items() if k.startswith(prefix)},sort_keys=True).encode()).hexdigest()
    manifest={"benchmark_version":"phase4-v2.0","frozen_at":datetime.now(timezone.utc).isoformat(),"git_sha":revision,"branch":branch,
         "git_status_short":status,"dirty_tree_source_hash":digest("app/"),"model":settings.model,
         "model_configuration":{"enable_thinking":False,"temperature":0,"top_p":"provider_default_not_explicitly_set",
                                "provider_base_sha256":hashlib.sha256((settings.api_base or "").encode()).hexdigest()},
         "runtime_limits":{"max_tool_calls":MAX_TOOL_CALLS,"max_replans":MAX_REPLANS,"task_timeout_seconds":TASK_TIMEOUT,
                           "max_iterations":32,"consecutive_failures":3,"decision_timeout_seconds":35,"decision_max_tokens":2400},
         "decision_prompt_hash":sha(ROOT/"app/agents/decision_node.py"),"grounded_response_prompt_hash":sha(ROOT/"app/services/grounded_response.py"),
         "skill_definitions_hash":digest("skills/"),"tool_registry_hash":sha(ROOT/"app/tools/registry.py"),
         "fixture_hashes":{k:v for k,v in files.items() if "/fixtures/" in k},"gold_hash":sha(REPORT/"gold/cases.jsonl"),
         "database_snapshot":snapshot_hash(),"frontend_build_hash":digest("web/dist/"),"frontend_source_hash":digest("web/src/"),
         "dependencies":versions,"formal_case_count":60,"concurrency":4,"cache":"fresh conversations; no cross-case result cache",
         "variants":["FULL","NO_SKILL","NO_REPLAN","STATELESS_FOLLOWUP","NO_EVIDENCE_GATE"],
         "source_hashes":files,"gold_exposure":"only evaluator; UI driver sends queries and uploaded fixtures, never expected labels",
         "review":"agent-authored synthetic gold with programmatic oracle; no independent human review claimed",
         "judge":"separate fixed-rubric evaluator, qwen3.7-flash; semantic metrics marked judge-assisted"}
    dump(REPORT/"freeze_manifest.json",manifest)
    print(json.dumps({"frozen":manifest["benchmark_version"],"git_sha":revision,"model":settings.model,"source_files":len(files)}))


def verify():
    frozen=json.loads((REPORT/"freeze_manifest.json").read_text(encoding="utf-8"))
    changed=[key for key,value in frozen["source_hashes"].items() if not (ROOT/key).exists() or sha(ROOT/key)!=value]
    if changed:raise RuntimeError("Frozen files changed: "+str(changed))
    if snapshot_hash()["sha256"]!=frozen["database_snapshot"]["sha256"]:raise RuntimeError("DB snapshot changed")
    print("Freeze verified: product/prompts/harness/gold/fixtures/DB unchanged")


if __name__=="__main__":
    import sys
    verify() if "--verify" in sys.argv else freeze()
