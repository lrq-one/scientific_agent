from __future__ import annotations

import asyncio
import os
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from app.services.execution_context import execution_identity


class HITLState(TypedDict, total=False):
    query: str
    user_id: str
    thread_id: str
    datasource_id: str | None
    dataset_version: str
    status: str
    intent: dict[str, Any]
    resources: dict[str, Any]
    selected_skills: list[str]
    waiting_kind: str
    question: str
    answer: str
    task_id: str
    conversation_id: str


def require_dataset_version(state: HITLState) -> dict[str, Any]:
    if state.get("waiting_kind") == "file":
        supplied = interrupt({"question": state["question"], "field": "files"})
        return {"answer": str(supplied), "status": "file_ready"}
    version = state.get("dataset_version")
    if not version:
        version = interrupt(
            {
                "question": "请提供要检查的训练数据集版本（例如 train_v3）。",
                "field": "dataset_version",
            }
        )
    return {"dataset_version": str(version), "status": "ready"}


class CheckpointService:
    """Owns one LangGraph saver and the persistent HITL graph."""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url if database_url is not None else os.getenv("CHECKPOINT_DATABASE_URL")
        self.connection = None
        self.persistent = False
        self.checkpointer = self._create_saver()
        builder = StateGraph(HITLState)
        builder.add_node("require_dataset_version", require_dataset_version)
        builder.add_edge(START, "require_dataset_version")
        builder.add_edge("require_dataset_version", END)
        self.hitl_graph = builder.compile(checkpointer=self.checkpointer)

    def _create_saver(self):
        if not self.database_url:
            return InMemorySaver()
        try:
            import psycopg
            from psycopg.rows import dict_row
            from langgraph.checkpoint.postgres import PostgresSaver

            self.connection = psycopg.connect(
                self.database_url,
                autocommit=True,
                prepare_threshold=0,
                row_factory=dict_row,
            )
            saver = PostgresSaver(self.connection)
            saver.setup()
            # Checkpoint rows are lifecycle data and must never be visible to the read-only science role.
            try:
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
                    self.connection.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM agent_reader")
            except Exception:
                pass
            self.persistent = True
            return saver
        except Exception:
            self.connection = None
            self.persistent = False
            return InMemorySaver()

    @staticmethod
    def config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"hitl:{thread_id}"}}

    async def start_hitl(self, state: HITLState) -> dict[str, Any]:
        if self.database_url and not self.persistent:
            raise RuntimeError("configured PostgreSQL checkpoint store is unavailable; refusing volatile HITL")
        state = {**state, **execution_identity.get()}
        return await asyncio.to_thread(self.hitl_graph.invoke, state, self.config(state["thread_id"]))

    async def resume_hitl(self, thread_id: str, answer: str) -> dict[str, Any]:
        if self.database_url and not self.persistent:
            raise RuntimeError("configured PostgreSQL checkpoint store is unavailable; refusing volatile resume")
        stored = await asyncio.to_thread(self.checkpointer.get_tuple, self.config(thread_id))
        if stored is None:
            raise RuntimeError("HITL checkpoint not found")
        values = stored.checkpoint["channel_values"]
        identity = execution_identity.get()
        if values.get("thread_id") != thread_id or any(
            values.get(key) and values[key] != identity[key] for key in ("task_id", "conversation_id") if key in identity
        ):
            raise RuntimeError("HITL checkpoint identity mismatch")
        return await asyncio.to_thread(self.hitl_graph.invoke, Command(resume=answer), self.config(thread_id))

    def checkpoint_exists(self, thread_id: str) -> bool:
        if self.database_url and not self.persistent:
            raise RuntimeError("configured PostgreSQL checkpoint store is unavailable")
        return self.checkpointer.get_tuple(self.config(thread_id)) is not None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()


checkpoint_service = CheckpointService()

