"""Zero-provider forensics from immutable UI traces and read-only DB metadata."""
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "reports/phase4a_correctness_closure_20261009"
OUT = ROOT / "reports/phase4a_final_blocker_resolution_20261009"
FILES = ["app/services/text2sql.py", "app/services/query_scope.py", "app/tools/dispatcher.py",
         "app/agents/runtime.py", "app/services/grounded_response.py", "app/agents/decision_node.py",
         "app/agents/goal_coverage.py"]


def main():
    OUT.mkdir(exist_ok=True)
    target = OUT / "initial_forensics.json"
    if target.exists():
        raise RuntimeError("Initial forensic evidence exists; refusing overwrite")
    cases = {}
    hashes = {}
    with psycopg.connect("postgresql://agent_reader:reader_demo@127.0.0.1:55432/phase4a_p0_development_20261008",
                         row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        columns = connection.execute("SELECT table_name,column_name,data_type FROM information_schema.columns "
                                     "WHERE table_schema='public' ORDER BY table_name,ordinal_position").fetchall()
        constraints = connection.execute("SELECT conrelid::regclass::text AS table_name,pg_get_constraintdef(oid) AS definition "
                                         "FROM pg_constraint WHERE connamespace='public'::regnamespace "
                                         "AND conrelid='training_memberships'::regclass").fetchall()
        for case in ("D09", "D08", "M02", "D06"):
            path = OLD / "ui" / f"closure-{case}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
            turn = record["turns"][0]
            state = [event["payload_json"]["state"] for event in turn["events"]
                     if event["event_type"] == "FINAL_ANSWER"][-1]
            cases[case] = {"conversation_id": record["conversation_id"], "task": turn["task"],
                           "events": turn["events"], "evidence": turn["evidence"],
                           "artifacts": turn["artifacts"], "state": state,
                           "failed_candidate_available": any(not result["success"] and
                               (result.get("metadata", {}).get("sql_candidate") or
                                isinstance(result.get("data"), dict) and result["data"].get("sql"))
                               for call, result in zip(state["tool_calls"], state["observations"])
                               if call["tool"] == "text_to_sql")}
        version = cases["D06"]["state"]["query_scope"]["dataset_version_id"]
        # This verifies counting semantics/uniqueness, not a new Agent execution.
        d06 = connection.execute("SELECT split,count(*) AS memberships,count(DISTINCT molecule_id) AS distinct_molecules "
                                 "FROM training_memberships WHERE dataset_version_id=%s GROUP BY split ORDER BY split", (version,)).fetchall()
        total = connection.execute("SELECT count(*) AS memberships,count(DISTINCT molecule_id) AS distinct_molecules "
                                   "FROM training_memberships WHERE dataset_version_id=%s", (version,)).fetchone()
    hashes.update({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES})
    result = {"new_llm_calls": 0, "label": "INITIAL READ-ONLY FORENSICS, NO NEW E2E",
              "cases": cases, "actual_columns": columns, "membership_constraints": constraints,
              "d06_count_semantics": {"groups": d06, "total": total}}
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "initial_source_manifest.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    print(json.dumps({"new_llm_calls": 0, "failed_sql_candidates_preserved":
                      {case: cases[case]["failed_candidate_available"] for case in cases},
                      "d06_count_semantics": result["d06_count_semantics"],
                      "membership_constraints": constraints}, indent=2))


if __name__ == "__main__":
    main()
