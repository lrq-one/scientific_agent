from __future__ import annotations

import os
from typing import Any
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class ConversationNotFound(LookupError):
    pass


class ActiveTaskConflict(RuntimeError):
    pass


def deterministic_title(query: str, limit: int = 36) -> str:
    """Create a stable first-query title without an LLM call."""
    title = " ".join(query.strip().split())
    return title if len(title) <= limit else f"{title[: limit - 1]}…"


class ConversationRepository:
    """PostgreSQL source of truth for user-visible history and task traces."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._ensure_followup_schema()

    def _ensure_followup_schema(self) -> None:
        with self.connect() as connection:
            connection.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check")
            connection.execute("""ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
                CHECK (status IN ('running', 'waiting_for_user', 'cancelling', 'cancelled', 'completed', 'failed'))""")
            connection.execute("""CREATE INDEX IF NOT EXISTS tasks_active_per_conversation_idx
                ON tasks (conversation_id) WHERE status IN ('running', 'waiting_for_user', 'cancelling')""")
            connection.execute("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS dataset_version text")
            connection.execute("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS model_version text")
            connection.execute("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS local_evidence_id text")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS claims (
                  id uuid PRIMARY KEY,
                  task_id uuid NOT NULL REFERENCES tasks(id),
                  claim_text text NOT NULL,
                  status text NOT NULL CHECK (status IN ('supported', 'unsupported')),
                  category text NOT NULL CHECK (category IN ('observation', 'interpretation')),
                  evidence_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
                  created_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS claims_task_created_idx ON claims (task_id, created_at)")

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def create(self, user_id: str, title: str = "新建科研任务") -> dict[str, Any]:
        conversation_id = str(uuid.uuid4())
        with self.connect() as connection:
            return dict(
                connection.execute(
                    """
                    INSERT INTO conversations (id, user_id, title)
                    VALUES (%s, %s, %s)
                    RETURNING id::text, user_id, title, created_at, updated_at, archived_at
                    """,
                    (conversation_id, user_id, title.strip()),
                ).fetchone()
            )

    def list(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id::text, c.user_id, c.title, c.created_at, c.updated_at,
                       count(m.id)::int AS message_count,
                       (SELECT t.status FROM tasks t WHERE t.conversation_id = c.id
                        ORDER BY t.started_at DESC, t.id DESC LIMIT 1) AS latest_task_status,
                       (SELECT t.id::text FROM tasks t WHERE t.conversation_id = c.id
                        AND t.status IN ('running', 'waiting_for_user', 'cancelling')
                        ORDER BY t.started_at DESC LIMIT 1) AS active_task_id
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = %s AND c.archived_at IS NULL
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def require(self, conversation_id: str, user_id: str, include_archived: bool = False) -> dict[str, Any]:
        archived_clause = "" if include_archived else "AND archived_at IS NULL"
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT id::text, user_id, title, created_at, updated_at, archived_at
                FROM conversations WHERE id = %s AND user_id = %s {archived_clause}
                """,
                (conversation_id, user_id),
            ).fetchone()
        if not row:
            raise ConversationNotFound(conversation_id)
        return dict(row)

    def get(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            conversation = connection.execute(
                """
                SELECT id::text, user_id, title, created_at, updated_at, archived_at
                FROM conversations
                WHERE id = %s AND user_id = %s AND archived_at IS NULL
                """,
                (conversation_id, user_id),
            ).fetchone()
            if not conversation:
                raise ConversationNotFound(conversation_id)
            messages = connection.execute(
                """
                SELECT id::text, conversation_id::text, task_id::text, role, content, created_at
                FROM messages WHERE conversation_id = %s ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
            tasks = connection.execute(
                """
                SELECT id::text, conversation_id::text, thread_id, status, intent_json,
                       selected_skills_json, started_at, finished_at
                FROM tasks WHERE conversation_id = %s ORDER BY started_at
                """,
                (conversation_id,),
            ).fetchall()
            task_ids = [row["id"] for row in tasks]
            if task_ids:
                events = connection.execute(
                    """
                    SELECT id, task_id::text, event_type, payload_json, created_at
                    FROM task_events WHERE task_id = ANY(%s::uuid[]) ORDER BY created_at, id
                    """,
                    (task_ids,),
                ).fetchall()
                evidence = connection.execute(
                    """
                    SELECT id::text, task_id::text, claim, value_json, source_type, source,
                           tool_call_id, local_evidence_id, dataset_version, model_version, created_at
                    FROM evidence WHERE task_id = ANY(%s::uuid[]) ORDER BY created_at
                    """,
                    (task_ids,),
                ).fetchall()
                artifacts = connection.execute(
                    """
                    SELECT id::text AS artifact_id, task_id::text, artifact_type, object_key,
                           filename, metadata_json AS metadata, created_at
                    FROM artifacts WHERE task_id = ANY(%s::uuid[]) ORDER BY created_at
                    """,
                    (task_ids,),
                ).fetchall()
                claims = connection.execute(
                    """
                    SELECT id::text, task_id::text, claim_text, status, category,
                           evidence_ids_json, created_at
                    FROM claims WHERE task_id = ANY(%s::uuid[]) ORDER BY created_at, id
                    """,
                    (task_ids,),
                ).fetchall()
            else:
                events, evidence, artifacts, claims = [], [], [], []
        conversation = dict(conversation)
        conversation.update(
            messages=[dict(row) for row in messages],
            tasks=[dict(row) for row in tasks],
            events=[dict(row) for row in events],
            evidence=[dict(row) for row in evidence],
            claims=[dict(row) for row in claims],
            artifacts=[dict(row) for row in artifacts],
        )
        return conversation

    def rename(self, conversation_id: str, user_id: str, title: str) -> dict[str, Any]:
        self.require(conversation_id, user_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE conversations SET title = %s, updated_at = now()
                WHERE id = %s AND user_id = %s
                RETURNING id::text, user_id, title, created_at, updated_at, archived_at
                """,
                (title.strip(), conversation_id, user_id),
            ).fetchone()
        return dict(row)

    def archive(self, conversation_id: str, user_id: str) -> None:
        self.require(conversation_id, user_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE conversations SET archived_at = now(), updated_at = now() WHERE id = %s AND user_id = %s",
                (conversation_id, user_id),
            )

    def messages(self, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        self.require(conversation_id, user_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id::text, conversation_id::text, task_id::text, role, content, created_at
                FROM messages WHERE conversation_id = %s ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_analysis_context(self, conversation_id: str, user_id: str) -> dict[str, Any] | None:
        contexts = self.recent_analysis_contexts(conversation_id, user_id, limit=1)
        return contexts[0] if contexts else None

    def recent_analysis_contexts(
        self, conversation_id: str, user_id: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Return bounded recent analysis tasks, including failed tasks without Evidence.

        Explanation-only follow-ups are skipped, but an analysis with missing data
        remains the most recent task. This prevents reuse of older Evidence.
        """
        detail = self.get(conversation_id, user_id)
        events_by_task: dict[str, list[dict[str, Any]]] = {}
        evidence_by_task: dict[str, list[dict[str, Any]]] = {}
        artifacts_by_task: dict[str, list[dict[str, Any]]] = {}
        claims_by_task: dict[str, list[dict[str, Any]]] = {}
        messages_by_task: dict[str, list[dict[str, Any]]] = {}
        for item in detail["events"]:
            events_by_task.setdefault(item["task_id"], []).append(item)
        for item in detail["evidence"]:
            evidence_by_task.setdefault(item["task_id"], []).append(item)
        for item in detail["artifacts"]:
            artifacts_by_task.setdefault(item["task_id"], []).append(item)
        for item in detail["claims"]:
            claims_by_task.setdefault(item["task_id"], []).append(item)
        for item in detail["messages"]:
            if item.get("task_id"):
                messages_by_task.setdefault(item["task_id"], []).append(item)
        contexts: list[dict[str, Any]] = []
        eligible = [task for task in detail["tasks"] if task["status"] in {"completed", "failed", "waiting_for_user"}
                    and (task.get("intent_json") or {}).get("requires_scientific_execution") is not False
                    and (task.get("intent_json") or {}).get("follow_up_type") not in
                    {"EVIDENCE_EXPLANATION", "RESULT_EXPLANATION", "ERROR_QUESTION"}
                    and (task.get("intent_json") or {}).get("interaction_type") not in
                    {"GREETING", "IDENTITY_QUESTION", "CAPABILITY_QUESTION", "SECURITY_REFUSAL", "CLARIFY"}
                    and not ((task.get("intent_json") or {}).get("task_type") == "general"
                             and not evidence_by_task.get(task["id"])
                             and not any(event["event_type"] == "TOOL_FINISHED" for event in events_by_task.get(task["id"], [])))]
        first_id = eligible[0]["id"] if eligible else None
        for task in reversed(eligible):
            if task["status"] not in {"completed", "failed", "waiting_for_user"}:
                continue
            follow_up_type = (task.get("intent_json") or {}).get("follow_up_type")
            if (task.get("intent_json") or {}).get("requires_scientific_execution") is False:
                continue
            if follow_up_type in {"EVIDENCE_EXPLANATION", "RESULT_EXPLANATION", "ERROR_QUESTION"}:
                continue
            task_events = events_by_task.get(task["id"], [])
            assistant = next(
                (item for item in reversed(messages_by_task.get(task["id"], [])) if item["role"] in {"assistant", "error"}),
                None,
            )
            contexts.append({
                "task": task,
                "events": task_events,
                "evidence": evidence_by_task.get(task["id"], []),
                "artifacts": artifacts_by_task.get(task["id"], []),
                "claims": claims_by_task.get(task["id"], []),
                "messages": messages_by_task.get(task["id"], []),
                "assistant_message": assistant,
                "is_first_substantive": task["id"] == first_id,
            })
            if len(contexts) >= limit:
                break
        return contexts

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO messages (id, conversation_id, task_id, role, content)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id::text, conversation_id::text, task_id::text, role, content, created_at
                """,
                (message_id, conversation_id, task_id, role, content),
            ).fetchone()
            connection.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = %s",
                (conversation_id,),
            )
        return dict(row)

    def set_first_query_title(self, conversation_id: str, query: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE conversations SET title = %s, updated_at = now()
                WHERE id = %s AND NOT EXISTS (
                  SELECT 1 FROM messages WHERE conversation_id = %s AND role = 'user'
                )
                """,
                (deterministic_title(query), conversation_id, conversation_id),
            )

    def start_task(self, conversation_id: str, thread_id: str) -> str:
        task_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"thread:{thread_id}",))
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (conversation_id,))
            active = connection.execute(
                """SELECT 1 FROM tasks WHERE thread_id = %s OR
                   (conversation_id = %s AND status IN ('running', 'waiting_for_user', 'cancelling')) LIMIT 1""",
                (thread_id, conversation_id),
            ).fetchone()
            if active:
                raise ActiveTaskConflict(conversation_id)
            connection.execute(
                """
                INSERT INTO tasks (id, conversation_id, thread_id, status)
                VALUES (%s, %s, %s, 'running')
                """,
                (task_id, conversation_id, thread_id),
            )
        return task_id

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        intent: dict[str, Any] | None = None,
        selected_skills: list[str] | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if status is not None:
            assignments.append("status = %s")
            params.append(status)
            if status in {"completed", "failed", "cancelled"}:
                assignments.append("finished_at = now()")
        if intent is not None:
            assignments.append("intent_json = %s")
            params.append(Jsonb(intent))
        if selected_skills is not None:
            assignments.append("selected_skills_json = %s")
            params.append(Jsonb(selected_skills))
        if not assignments:
            return
        params.append(task_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = %s", params)

    def claim_resume(self, task_id: str, conversation_id: str, thread_id: str) -> bool:
        """Atomically reserve a waiting task; duplicate/concurrent/expired resumes fail."""
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE tasks SET status = 'running'
                WHERE id = %s AND conversation_id = %s AND thread_id = %s
                  AND status = 'waiting_for_user'
                  AND started_at > now() - interval '7 days'
                RETURNING id
                """,
                (task_id, conversation_id, thread_id),
            ).fetchone()
        return row is not None

    def cancel_waiting_task(self, task_id: str, conversation_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE tasks SET status = CASE WHEN status = 'waiting_for_user' THEN 'cancelled' ELSE 'cancelling' END,
                                 finished_at = CASE WHEN status = 'waiting_for_user' THEN now() ELSE NULL END
                WHERE id = %s AND conversation_id = %s AND status IN ('running', 'waiting_for_user')
                RETURNING id, status
                """,
                (task_id, conversation_id),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (%s, %s, %s)",
                    (task_id, "CANCELLED" if row["status"] == "cancelled" else "CANCELLING",
                     Jsonb({"message": "用户已请求取消", "task_id": task_id})),
                )
        return row is not None

    def finish_cancellation(self, task_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = now() WHERE id = %s AND status = 'cancelling' RETURNING id",
                (task_id,),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (%s, 'CANCELLED', %s)",
                    (task_id, Jsonb({"message": "任务已取消", "task_id": task_id})),
                )
        return row is not None

    def is_cancelled(self, task_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE id = %s AND status IN ('cancelling', 'cancelled') LIMIT 1",
                (task_id,),
            ).fetchone()
        return row is not None

    def active_task(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id::text, conversation_id::text, thread_id, status, intent_json,
                       selected_skills_json, started_at, finished_at
                FROM tasks
                WHERE conversation_id = %s
                  AND status IN ('running', 'waiting_for_user', 'cancelling')
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def task_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id::text, conversation_id::text, thread_id, status, intent_json,
                       selected_skills_json, started_at, finished_at
                FROM tasks WHERE thread_id = %s
                ORDER BY started_at DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_orphaned_tasks(self) -> dict[str, int]:
        """Fail-safe reconciliation after a FastAPI process restart.

        In-process asyncio workers cannot survive interpreter shutdown. Running
        tasks are therefore never left pretending to be alive after restart.
        Waiting HITL tasks remain resumable from PostgreSQL/checkpoint state.
        """
        with self.connect() as connection:
            cancelling = connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', finished_at = now()
                WHERE status = 'cancelling'
                RETURNING id
                """
            ).fetchall()
            running = connection.execute(
                """
                UPDATE tasks
                SET status = 'failed', finished_at = now()
                WHERE status = 'running'
                RETURNING id
                """
            ).fetchall()
            for row in cancelling:
                connection.execute(
                    "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (%s, 'CANCELLED', %s)",
                    (row["id"], Jsonb({
                        "message": "服务重启时任务处于取消中，已安全终止",
                        "error_type": "ProcessRestarted",
                    })),
                )
            for row in running:
                connection.execute(
                    "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (%s, 'ERROR', %s)",
                    (row["id"], Jsonb({
                        "message": "服务重启中断了内存执行器；任务已标记失败，可从原会话重新运行",
                        "error_type": "ProcessRestarted",
                        "recoverable": True,
                    })),
                )
        return {"failed_running": len(running), "cancelled_cancelling": len(cancelling)}

    def task_status(self, task_id: str, conversation_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = %s AND conversation_id = %s", (task_id, conversation_id)
            ).fetchone()
        return row["status"] if row else None

    def owned_task_conversation(self, task_id: str, user_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT t.conversation_id::text AS conversation_id FROM tasks t
                   JOIN conversations c ON c.id = t.conversation_id
                   WHERE t.id = %s AND c.user_id = %s AND c.archived_at IS NULL""",
                (task_id, user_id),
            ).fetchone()
        return row["conversation_id"] if row else None

    def conversation_for_task(self, task_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT conversation_id::text AS conversation_id FROM tasks WHERE id = %s", (task_id,)
            ).fetchone()
        return row["conversation_id"] if row else None

    def events_after(self, task_id: str, after_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, event_type, payload_json FROM task_events WHERE task_id = %s AND id > %s ORDER BY id",
                (task_id, after_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_event_id(self, task_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(id), 0) AS id FROM task_events WHERE task_id = %s", (task_id,)
            ).fetchone()
        return int(row["id"])

    def finish_task_with_answer(
        self, task_id: str, conversation_id: str, answer: str,
        payload: dict[str, Any], *, status: str = "completed",
    ) -> bool:
        """Persist a terminal answer only if the task was not cancelled."""
        if status not in {"completed", "failed"}:
            raise ValueError("invalid terminal status")
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE tasks SET status = %s, finished_at = now()
                WHERE id = %s AND conversation_id = %s AND status = 'running'
                RETURNING id
                """,
                (status, task_id, conversation_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (%s, 'FINAL_ANSWER', %s)",
                (task_id, Jsonb(payload)),
            )
            connection.execute(
                "INSERT INTO messages (id, conversation_id, task_id, role, content) VALUES (%s, %s, %s, 'assistant', %s)",
                (str(uuid.uuid4()), conversation_id, task_id, answer),
            )
            connection.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
        return True

    def has_thread(self, thread_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM tasks WHERE thread_id = %s LIMIT 1", (thread_id,)).fetchone()
        return row is not None

    def add_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, payload_json)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (task_id, event_type, Jsonb(payload)),
            ).fetchone()
        return int(row["id"])

    def add_evidence(self, task_id: str, item: dict[str, Any]) -> str:
        evidence_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evidence (
                  id, task_id, claim, value_json, source_type, source, tool_call_id, local_evidence_id,
                  dataset_version, model_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    evidence_id,
                    task_id,
                    item["claim"],
                    Jsonb(item.get("value")),
                    item["source_type"],
                    item["source"],
                    item["tool_call_id"],
                    item.get("evidence_id"),
                    item.get("dataset_version"),
                    item.get("model_version"),
                ),
            )
        return evidence_id

    def add_claims(self, task_id: str, claims: list[dict[str, Any]]) -> list[str]:
        """Persist explicit claim-to-Evidence links using stable database IDs."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id::text, local_evidence_id FROM evidence WHERE task_id = %s",
                (task_id,),
            ).fetchall()
            evidence_ids = {row["local_evidence_id"]: row["id"] for row in rows if row["local_evidence_id"]}
            saved: list[str] = []
            for claim in claims:
                linked = [evidence_ids[item] for item in claim.get("evidence_ids", []) if item in evidence_ids]
                status = claim.get("status", "unsupported")
                if status == "supported" and len(linked) != len(claim.get("evidence_ids", [])):
                    status = "unsupported"
                if status == "supported" and not linked:
                    status = "unsupported"
                claim_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO claims (id, task_id, claim_text, status, category, evidence_ids_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (claim_id, task_id, claim["text"], status,
                     claim.get("category", "observation"), Jsonb(linked)),
                )
                saved.append(claim_id)
        return saved

    def add_artifact(self, task_id: str, artifact: dict[str, Any]) -> str:
        artifact_id = artifact.get("artifact_id") or str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (id, task_id, artifact_type, object_key, filename, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    artifact_id,
                    task_id,
                    artifact["artifact_type"],
                    artifact["object_key"],
                    artifact["filename"],
                    Jsonb(artifact.get("metadata", {})),
                ),
            )
        return artifact_id

    def artifact(self, artifact_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.id::text AS artifact_id, a.artifact_type, a.object_key, a.filename,
                       a.metadata_json AS metadata
                FROM artifacts a
                JOIN tasks t ON t.id = a.task_id
                JOIN conversations c ON c.id = t.conversation_id
                WHERE a.id = %s AND c.user_id = %s AND c.archived_at IS NULL
                """,
                (artifact_id, user_id),
            ).fetchone()
        if not row:
            raise ConversationNotFound(artifact_id)
        return dict(row)


_repository: ConversationRepository | None = None
_repository_url: str | None = None


def conversation_repository() -> ConversationRepository | None:
    global _repository, _repository_url
    url = os.getenv("ADMIN_DATABASE_URL")
    if not url:
        return None
    if _repository is None or _repository_url != url:
        _repository = ConversationRepository(url)
        _repository_url = url
    return _repository
