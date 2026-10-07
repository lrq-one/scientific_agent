"""Read-only diagnostic for the latest followup-real smoke conversation."""

import json
import os

import psycopg


with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as connection:
    selected_task = os.getenv("PHASE45_TASK_ID")
    task = connection.execute("""
        SELECT t.id::text, t.status, t.thread_id, c.id::text
        FROM tasks t JOIN conversations c ON c.id = t.conversation_id
        WHERE c.user_id LIKE 'followup-real-%%' AND (%s::uuid IS NULL OR t.id = %s::uuid)
        ORDER BY t.started_at DESC LIMIT 1
    """, (selected_task, selected_task)).fetchone()
    if task:
        events = connection.execute("""
            SELECT event_type, payload_json FROM task_events
            WHERE task_id = %s ORDER BY id
        """, (task[0],)).fetchall()
        evidence = connection.execute("SELECT count(*) FROM evidence WHERE task_id = %s", (task[0],)).fetchone()[0]
        claims = connection.execute("SELECT claim_text, status, evidence_ids_json FROM claims WHERE task_id = %s", (task[0],)).fetchall()
        output = []
        for event_type, payload in events:
            result = payload.get("result") or {}
            output.append({
                "event": event_type,
                "tool": payload.get("tool"),
                "success": result.get("success"),
                "error": payload.get("error") or result.get("error"),
                "generator": (result.get("metadata") or {}).get("generator"),
                "candidate": result.get("data") if payload.get("tool") == "text_to_sql" else None,
                "rows": result.get("data") if payload.get("tool") == "execute_readonly_sql" else None,
                "question": payload.get("question"),
                "quality_status": (payload.get("state") or {}).get("quality_status") if event_type == "FINAL_ANSWER" else None,
            })
        print(json.dumps({"task_id": task[0], "status": task[1], "thread_id": task[2],
                          "conversation_id": task[3], "evidence_count": evidence,
                          "claims": [{"text": row[0][:160], "status": row[1], "evidence_ids": row[2]} for row in claims],
                          "events": output}, ensure_ascii=False, indent=2, default=str))
