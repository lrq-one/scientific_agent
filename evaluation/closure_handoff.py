"""Read-only final diagnostics: no provider, Agent, SQL, or tool execution."""
import hashlib
import json
import subprocess
from pathlib import Path

from app.agents.goal_coverage import assess_goal_coverage
from app.models.schemas import ScientificAgentState

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/phase4a_correctness_closure_20261009"


def main():
    record = json.loads((OUT / "ui/closure-D06.json").read_text(encoding="utf-8"))
    saved = [event["payload_json"]["state"] for event in record["turns"][0]["events"]
             if event["event_type"] == "FINAL_ANSWER"][-1]
    state = ScientificAgentState.model_validate(saved)
    coverage = assess_goal_coverage(state)
    assert coverage.status == "SATISFIED" and coverage.required_dimensions == ["split"]
    result = {"label": "POST-SMOKE OFFLINE REPLAY, NOT A FRESH E2E SUCCESS",
              "task_id": state.task_id, "before": saved["goal_coverage"], "after": coverage.model_dump(),
              "identity_unchanged": (state.task_id, state.thread_id, state.conversation_id) ==
                                    (saved["task_id"], saved["thread_id"], saved["conversation_id"]),
              "new_llm_calls": 0, "new_tool_calls": 0}
    (OUT / "post_smoke_replay.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    before = json.loads((OUT / "smoke_source_manifest.json").read_text(encoding="utf-8"))
    paths = list(before) + ["evaluation/closure_handoff.py", "evaluation/closure_smoke_summary.py",
                           "evaluation/correctness_replay.py", "scripts/run-correctness-closure.ps1",
                           "web/scripts/correctnessClosure.mjs"]
    final = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
    (OUT / "source_manifest.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
    git = [r"G:\Git\cmd\git.exe", "-c", "core.quotepath=false"]
    status = subprocess.run(git + ["status", "--short"], cwd=ROOT, check=True, capture_output=True)
    (OUT / "git_status.txt").write_bytes(status.stdout)
    branch = subprocess.run(git + ["branch", "--show-current"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(git + ["diff", "--check"], cwd=ROOT, capture_output=True, text=True)
    checks = {"branch": branch, "git_diff_check_exit": diff.returncode,
              "post_smoke_changed_sources": [name for name in before if before[name] != final[name]],
              "post_smoke_llm_calls": 0, "post_smoke_tool_calls": 0,
              "paid_smoke_not_rerun": True, "p2_ready": False}
    namespace = json.loads((ROOT / "reports/phase4a_p0_hardening_20261008/development_namespace.json")
                           .read_text(encoding="utf-8"))
    changed = [name for name, digest in namespace["original_report_hashes"].items()
               if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    checks["original_frozen_report_hash_changes"] = changed
    assert not changed, "Historical reports changed; do not overwrite their records"
    costs = json.loads((OUT / "cost_summary.json").read_text(encoding="utf-8"))
    assert costs["new_qwen_attempts"] == costs["finished_attempts"] == 53
    assert costs["unfinished_attempts"] == costs["missing_usage"] == 0
    (OUT / "handoff_checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    return diff.returncode


if __name__ == "__main__":
    raise SystemExit(main())
