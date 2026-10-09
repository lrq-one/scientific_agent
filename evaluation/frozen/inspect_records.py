"""Read-only PostgreSQL validation; no Agent/tool/LLM execution."""
import json
import psycopg
from psycopg.rows import dict_row
from evaluation.frozen.prepare import REPORT,dump


def main():
    with psycopg.connect("postgresql://scientific:scientific@127.0.0.1:55432/phase4_eval_v2",row_factory=dict_row) as db:
        db.execute("SET TRANSACTION READ ONLY")
        for variant in (REPORT/"runs").iterdir():
            if not variant.is_dir():continue
            for p in variant.glob("*.json"):
                r=json.loads(p.read_text(encoding="utf-8"))
                if not r.get("conversation_id"):continue
                conv=r["conversation_id"]
                conversation=db.execute("SELECT id::text,user_id,archived_at FROM conversations WHERE id=%s",(conv,)).fetchone()
                tasks=[]
                for turn in r.get("turns",[]):
                    task=turn.get("task")
                    if not task:continue
                    key=f"agent:{task['thread_id']}:{task['id']}"
                    checkpoints=db.execute("SELECT thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id",(key,)).fetchall()
                    counts={table:db.execute(f"SELECT count(*) AS n FROM {table} WHERE task_id=%s",(task["id"],)).fetchone()["n"]
                            for table in ["messages","task_events","evidence","claims","artifacts"]}
                    tasks.append({"task_id":task["id"],"thread_id":task["thread_id"],"expected_checkpoint_key":key,
                                  "checkpoints":checkpoints,"counts":counts,"checkpoint_saved":bool(checkpoints)})
                dump(REPORT/"inspections"/variant.name/(r["case_id"]+".json"),{"conversation":conversation,"tasks":tasks,"read_only":True})
    print("Read-only checkpoint and association inspections saved")


if __name__=="__main__":
    import sys
    if "--active-count" in sys.argv:
        with psycopg.connect("postgresql://scientific:scientific@127.0.0.1:55432/phase4_eval_v2") as db:
            print(db.execute("SELECT count(*) FROM tasks WHERE status IN ('running','cancelling')").fetchone()[0])
    else:
        main()
