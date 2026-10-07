from __future__ import annotations

import json
import os
import re
from time import perf_counter
from typing import Any, Protocol

from rank_bm25 import BM25Okapi

from app.models.schemas import SQLCandidate
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector


def tokenize(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]", text.lower())
    words: list[str] = []
    for token in raw:
        parts = [token, *token.split("_")] if "_" in token else [token]
        for part in parts:
            if not part:
                continue
            words.append(part[:-1] if len(part) > 3 and part.endswith("s") else part)
    return words or [text.lower()]


class SchemaRetrieverProtocol(Protocol):
    def search(
        self,
        goal: str,
        schema: dict[str, list[dict[str, Any]]],
        relationships: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]: ...


class SchemaRetriever:
    """BM25 implementation behind a stable interface for future dense/hybrid retrieval."""

    def __init__(self, top_k: int | None = None):
        self.top_k = top_k or int(os.getenv("SCHEMA_TOP_K", "5"))

    def search(
        self,
        goal: str,
        schema: dict[str, list[dict[str, Any]]],
        relationships: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        if not schema:
            return []
        tables = list(schema)
        documents = []
        for table in tables:
            columns = schema[table]
            table_description = next((column.get("table_description", "") for column in columns), "")
            text = " ".join(
                [table, table_description]
                + [f"{column['name']} {column.get('description', '')}" for column in columns]
            )
            documents.append(tokenize(text))
        query_tokens = tokenize(goal)
        bm25_scores = BM25Okapi(documents).get_scores(query_tokens)
        # Exact lexical overlap stabilizes very small schemas where Okapi IDF can
        # be negative for a term present in half of the documents.
        scores = [
            float(score) + len(set(query_tokens) & set(document))
            for score, document in zip(bm25_scores, documents, strict=True)
        ]
        ranked = sorted(zip(tables, scores), key=lambda item: (-float(item[1]), item[0]))[: self.top_k]
        relations = relationships or []
        return [
            {
                "table": table,
                "columns": schema[table],
                "relationships": [
                    relation
                    for relation in relations
                    if relation.get("source_table") == table or relation.get("target_table") == table
                ],
                "score": round(float(score), 6),
            }
            for table, score in ranked
        ]

    def retrieve(self, goal: str, schema: dict[str, list[dict[str, Any]]], limit: int = 3) -> dict[str, list[dict[str, Any]]]:
        if not schema:
            return {}
        hits = SchemaRetriever(top_k=limit).search(goal, schema)
        selected = [hit["table"] for hit in hits if hit["score"] > 0]
        if not selected:
            selected = ["training_molecules"] if "training_molecules" in schema else list(schema)[:limit]
        return {table: schema[table] for table in selected}


class TextToSQLService:
    def __init__(self, llm: Any | None = None):
        self.llm = llm
        self.retriever = SchemaRetriever()

    def _configured_llm(self):
        if self.llm is not None:
            return self.llm
        settings = llm_settings()
        if not settings.configured:
            return None
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=0,
            extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
        )

    def prompt(
        self,
        goal: str,
        current_step: str,
        datasource: str,
        schema: dict[str, list[dict[str, str]]],
        relationships: list[dict[str, str]],
        dataset_version: str | None = None,
        repair_feedback: str | None = None,
    ) -> str:
        dataset_constraint = ""
        if dataset_version:
            dataset_constraint = (
                f"Dataset version: {dataset_version}. The query must constrain results to this exact "
                "dataset version using the appropriate version table/column, the named placeholder "
                "%(dataset_version)s, and params.dataset_version.\n"
            )
        repair_constraint = (
            f"Previous candidate failed PostgreSQL validation: {repair_feedback[:300]}. "
            "Use only columns and aliases present in the supplied schema; return a corrected query.\n"
            if repair_feedback else ""
        )
        return (
            "Generate one parameterized PostgreSQL read-only query as SQLCandidate.\n"
            f"Goal: {goal}\nCurrent step: {current_step}\nDatasource: {datasource}\n"
            f"{dataset_constraint}"
            f"{repair_constraint}"
            f"Relevant schema: {json.dumps(schema, ensure_ascii=False)}\n"
            f"Relationships: {json.dumps(relationships, ensure_ascii=False)}\n"
            f"Allowed tables: {list(schema)}\n"
            "Constraints: exactly one SELECT or WITH SELECT; no DML/DDL; use named psycopg params; no comments."
        )

    async def generate(
        self,
        goal: str,
        current_step: str,
        datasource: str,
        full_schema: dict[str, list[dict[str, str]]],
        relationships: list[dict[str, str]],
        dataset_version: str | None = None,
        repair_feedback: str | None = None,
    ) -> tuple[SQLCandidate, dict[str, Any]]:
        relevant = self.retriever.retrieve(goal, full_schema)
        related_names = set(relevant)
        for relation in relationships:
            if relation.get("source_table") in related_names:
                related_names.add(relation.get("target_table", ""))
            if relation.get("target_table") in related_names:
                related_names.add(relation.get("source_table", ""))
        relevant = {name: full_schema[name] for name in related_names if name in full_schema}
        llm = self._configured_llm()
        if llm is not None:
            collector = UsageCollector()
            started = perf_counter()
            try:
                candidate = await llm.with_structured_output(SQLCandidate).ainvoke(
                    self.prompt(goal, current_step, datasource, relevant, relationships, dataset_version, repair_feedback),
                    config={"callbacks": [collector]},
                )
                metadata = {
                    "generator": "llm_structured_output", "relevant_tables": list(relevant),
                    "llm_telemetry": {
                        "llm_called": True, "model_configured": llm_settings().model,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        "http_status": None, "fallback": False, **collector.snapshot(),
                    },
                }
                if dataset_version and "%(dataset_version)s" not in candidate.sql:
                    raise ValueError("SQLCandidate did not bind the requested dataset_version")
                if dataset_version:
                    # The model chooses SQL structure; the trusted HITL/request value owns binding.
                    metadata["parameter_binding_repaired"] = candidate.params.get("dataset_version") != dataset_version
                    candidate.params["dataset_version"] = dataset_version
                return candidate, metadata
            except Exception as exc:
                fallback_reason = f"LLM unavailable after {type(exc).__name__}; deterministic fixture fallback"
                failure_telemetry = {
                    "llm_called": True, "model_configured": llm_settings().model,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                    "http_status": None, "fallback": True, "error_type": type(exc).__name__,
                    **collector.snapshot(),
                }
        else:
            fallback_reason = "LLM API not configured; deterministic fixture fallback"
            failure_telemetry = {"llm_called": False, "fallback": True}

        params: dict[str, Any] = {}
        where = ""
        if dataset_version:
            where = " WHERE tm.dataset_version = %(dataset_version)s"
            params["dataset_version"] = dataset_version
        candidate = SQLCandidate(
            sql=(
                "SELECT m.structure_type, COUNT(*) AS sample_count "
                "FROM training_molecules AS tm "
                "JOIN molecules AS m ON m.molecule_id = tm.molecule_id"
                f"{where} GROUP BY m.structure_type ORDER BY sample_count DESC"
            ),
            params=params,
            reason=fallback_reason,
        )
        return candidate, {"generator": "deterministic_fixture_fallback", "relevant_tables": list(relevant),
                           "llm_telemetry": failure_telemetry}

