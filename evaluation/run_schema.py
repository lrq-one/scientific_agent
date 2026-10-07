from __future__ import annotations

import argparse
import json
import os
from statistics import mean
from time import perf_counter
from typing import Any

import numpy as np

from app.datasources.postgres import PostgresDatasource, PostgresSchemaInspector
from app.services.text2sql import SchemaRetriever
from evaluation.metrics import schema_retrieval_metrics
from evaluation.phase4_common import (
    PROMPT_VERSION,
    create_run,
    empty_record,
    split_cases,
    telemetry_summary,
    validate_record,
)


EMBEDDING_MODEL = "BAAI/bge-m3"
RRF_K = 60


def document_text(
    table: str,
    columns: list[dict[str, Any]],
    relationships: list[dict[str, str]],
) -> str:
    table_description = next((str(item.get("table_description", "")) for item in columns), "")
    column_text = " ".join(
        f"{table}.{item['name']} {item.get('type', '')} {item.get('description', '')}" for item in columns
    )
    relation_text = " ".join(
        f"{item['source_table']}.{item['source_column']} references "
        f"{item['target_table']}.{item['target_column']}"
        for item in relationships
        if table in {item["source_table"], item["target_table"]}
    )
    return f"table {table}. {table_description}. columns: {column_text}. relationships: {relation_text}"


def public_hit(table: str, schema: dict[str, list[dict[str, Any]]], score: float) -> dict[str, Any]:
    return {
        "table": table,
        "columns": [f"{table}.{item['name']}" for item in schema[table]],
        "score": round(float(score), 8),
    }


def rrf_rank(bm25_tables: list[str], dense_tables: list[str]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in (bm25_tables, dense_tables):
        for rank, table in enumerate(ranking, 1):
            scores[table] = scores.get(table, 0.0) + 1 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def make_rows(
    cases: list[dict[str, Any]],
    split: str,
    method: str,
    schema: dict[str, list[dict[str, Any]]],
    rankings: list[list[tuple[str, float]]],
    latencies: list[float],
) -> list[dict[str, Any]]:
    rows = []
    for case, ranking, latency in zip(cases, rankings, latencies, strict=True):
        prediction = [public_hit(table, schema, score) for table, score in ranking]
        gold = {"gold_tables": case["gold_tables"], "gold_columns": case["gold_columns"]}
        top5_tables = {item["table"] for item in prediction[:5]}
        top5_columns = {column for item in prediction[:5] for column in item["columns"]}
        success = set(case["gold_tables"]) <= top5_tables and set(case["gold_columns"]) <= top5_columns
        row = empty_record(case["case_id"], split, "schema_retrieval", method, gold)
        row.update(
            model=EMBEDDING_MODEL if method in {"R1_bge_m3", "R2_hybrid_rrf"} else "BM25Okapi",
            success=success,
            prediction=prediction,
            candidate_count=len(schema),
            total_latency_ms=round(latency, 3),
            error_type=None if success else "retrieval_miss_at_5",
            api_calls=0,
            retrieval_only=True,
        )
        validate_record(row)
        rows.append(row)
    return rows


def evaluate(split: str, methods: list[str], database_url: str) -> None:
    cases = split_cases("schema_retrieval", split)
    inspector = PostgresSchemaInspector(PostgresDatasource("research", database_url))
    schema = inspector.tables()
    relationships = inspector.relationships()
    tables = sorted(schema)
    documents = [document_text(table, schema[table], relationships) for table in tables]

    bm25_rankings: list[list[tuple[str, float]]] = []
    bm25_latencies: list[float] = []
    retriever = SchemaRetriever(top_k=len(tables))
    for case in cases:
        started = perf_counter()
        hits = retriever.search(case["question"], schema, relationships)
        bm25_latencies.append((perf_counter() - started) * 1000)
        bm25_rankings.append([(item["table"], float(item["score"])) for item in hits])

    dense_rankings: list[list[tuple[str, float]]] = []
    dense_latencies: list[float] = []
    embedding_load_ms = 0.0
    document_index_ms = 0.0
    query_batch_ms = 0.0
    if any(method in {"R1_bge_m3", "R2_hybrid_rrf"} for method in methods):
        from sentence_transformers import SentenceTransformer

        started = perf_counter()
        model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
        embedding_load_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        document_vectors = model.encode(documents, normalize_embeddings=True, batch_size=4, show_progress_bar=False)
        document_index_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        query_vectors = model.encode(
            [case["question"] for case in cases], normalize_embeddings=True, batch_size=8, show_progress_bar=False
        )
        query_batch_ms = (perf_counter() - started) * 1000
        amortized_query_ms = query_batch_ms / max(1, len(cases))
        for query_vector in query_vectors:
            score_started = perf_counter()
            scores = np.asarray(document_vectors) @ np.asarray(query_vector)
            ranking = sorted(zip(tables, scores), key=lambda item: (-float(item[1]), item[0]))
            dense_latencies.append(amortized_query_ms + (perf_counter() - score_started) * 1000)
            dense_rankings.append([(table, float(score)) for table, score in ranking])

    by_method = {
        "R0_bm25": (bm25_rankings, bm25_latencies),
        "R1_bge_m3": (dense_rankings, dense_latencies),
    }
    if dense_rankings:
        by_method["R2_hybrid_rrf"] = (
            [
                rrf_rank([table for table, _ in lexical], [table for table, _ in dense])
                for lexical, dense in zip(bm25_rankings, dense_rankings, strict=True)
            ],
            [a + b for a, b in zip(bm25_latencies, dense_latencies, strict=True)],
        )
    for method in methods:
        rankings, latencies = by_method[method]
        rows = make_rows(cases, split, method, schema, rankings, latencies)
        result = telemetry_summary(rows)
        result.update(schema_retrieval_metrics([row["gold"] for row in rows], [row["prediction"] for row in rows]))
        result.update({
            "schema_tables": len(schema),
            "schema_columns": sum(map(len, schema.values())),
            "relationships": len(relationships),
            "embedding_model": EMBEDDING_MODEL if method != "R0_bm25" else None,
            "embedding_load_ms": round(embedding_load_ms, 3) if method != "R0_bm25" else 0.0,
            "document_index_ms": round(document_index_ms, 3) if method != "R0_bm25" else 0.0,
            "query_batch_ms": round(query_batch_ms, 3) if method != "R0_bm25" else 0.0,
        })
        config = {
            "stage": "schema_retrieval", "split": split, "method": method,
            "prompt_version": PROMPT_VERSION, "seed": 20261006,
            "embedding_model": EMBEDDING_MODEL if method != "R0_bm25" else None,
            "rrf_k": RRF_K if method == "R2_hybrid_rrf" else None,
            "database_backend": "postgres", "database_schema": "public",
        }
        create_run(f"schema_{split}_{method.lower()}", config, rows, result)
        print(method, json.dumps(result, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--methods", nargs="+", default=["R0_bm25", "R1_bge_m3", "R2_hybrid_rrf"])
    args = parser.parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for real PostgreSQL schema evaluation")
    evaluate(args.split, args.methods, database_url)


if __name__ == "__main__":
    main()
