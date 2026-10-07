from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from statistics import mean
from time import perf_counter
from typing import Any

import numpy as np

from app.datasources.postgres import PostgresDatasource, PostgresQueryExecutor, PostgresSchemaInspector
from app.models.schemas import SQLCandidate
from app.services.text2sql import SchemaRetriever
from app.tools.sql_guard import SQLGuard
from evaluation.phase4_common import (
    MODEL,
    PROMPT_VERSION,
    ResultCache,
    create_run,
    empty_record,
    split_cases,
    telemetry_summary,
    validate_record,
)
from evaluation.phase4_llm import RealLLMClient
from evaluation.run_schema import EMBEDDING_MODEL, document_text


ENABLE_THINKING = False
MAX_TOKENS = 2048
TOP_K = 5
FIXTURE_VERSION = "output-contract-v2"


def schema_prompt(schema: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        table: [
            {
                "name": item["name"],
                "type": item["type"],
                "description": item.get("description", ""),
                "primary_key": bool(item.get("primary_key")),
            }
            for item in columns
        ]
        for table, columns in schema.items()
    }


def related(relationships: list[dict[str, str]], tables: set[str]) -> list[dict[str, str]]:
    return [
        item for item in relationships
        if item["source_table"] in tables and item["target_table"] in tables
    ]


def prompt(case: dict[str, Any], schema: dict[str, list[dict[str, Any]]], relationships: list[dict[str, str]]) -> str:
    return (
        "Generate one PostgreSQL SQLCandidate that answers the question. Return SQL, params, and a short reason. "
        "Use only supplied tables and columns. Use exactly one read-only SELECT or WITH...SELECT statement; "
        "no comments, DML, DDL, or multiple statements. Return exactly the required output columns in the exact order.\n"
        f"Question: {case['question']}\n"
        f"Required output columns: {json.dumps(case['expected_result_signature'], ensure_ascii=False)}\n"
        f"Schema: {json.dumps(schema_prompt(schema), ensure_ascii=False)}\n"
        f"Foreign keys: {json.dumps(relationships, ensure_ascii=False)}"
    )


def repair_prompt(
    case: dict[str, Any],
    candidate: dict[str, Any],
    feedback: str,
    schema: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, str]],
) -> str:
    return (
        "Repair this PostgreSQL SQLCandidate once. Return a complete replacement SQLCandidate. "
        "Use only supplied tables/columns and exactly one read-only SELECT or WITH...SELECT; no comments or writes.\n"
        f"Question: {case['question']}\n"
        f"Required output columns: {json.dumps(case['expected_result_signature'], ensure_ascii=False)}\n"
        f"Previous candidate: {json.dumps(candidate, ensure_ascii=False)}\n"
        f"Checker feedback: {feedback}\n"
        f"Schema: {json.dumps(schema_prompt(schema), ensure_ascii=False)}\n"
        f"Foreign keys: {json.dumps(relationships, ensure_ascii=False)}"
    )


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round(float(value), 8)
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if type(value).__module__ == "uuid" else value


def canonical_rows(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(normalize(row), ensure_ascii=False, sort_keys=True) for row in rows)


class SQLRuntime:
    def __init__(self, database_url: str, full_schema: dict[str, list[dict[str, Any]]]):
        datasource = PostgresDatasource("research", database_url)
        self.executor = PostgresQueryExecutor(datasource)
        self.guard = SQLGuard()
        self.full_schema = full_schema

    def execute(self, candidate: SQLCandidate, allowed_tables: set[str]) -> tuple[str, list[dict[str, Any]]]:
        safe = self.guard.validate(candidate.sql, allowed_tables, dialect="postgres")
        self.executor.check(safe, candidate.params)
        return safe, self.executor.execute(safe, candidate.params)

    def gold(self, sql: str) -> list[dict[str, Any]]:
        safe = self.guard.validate(sql, set(self.full_schema), dialect="postgres")
        self.executor.check(safe, {})
        return self.executor.execute(safe, {})


def evaluate_candidate(
    runtime: SQLRuntime,
    candidate: SQLCandidate,
    allowed_tables: set[str],
    expected_rows: list[dict[str, Any]],
    expected_signature: list[str],
) -> dict[str, Any]:
    result = {
        "valid_sql": False, "security_pass": False, "execution_success": False,
        "result_match": False, "result_signature": [], "row_count": 0,
    }
    try:
        safe_sql, rows = runtime.execute(candidate, allowed_tables)
        result.update(valid_sql=True, security_pass=True, execution_success=True, safe_sql=safe_sql)
        result["row_count"] = len(rows)
        result["result_signature"] = list(rows[0]) if rows else expected_signature
        signature_match = not rows or result["result_signature"] == expected_signature
        result["result_match"] = signature_match and canonical_rows(rows) == canonical_rows(expected_rows)
        if not result["result_match"]:
            result["checker_feedback"] = (
                f"Query executed but answer mismatch. Required output columns: {expected_signature}; "
                f"actual columns: {result['result_signature']}. Re-check joins, grouping, ordering, and limits."
            )
    except Exception as exc:
        result["checker_feedback"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        result["runtime_error_type"] = type(exc).__name__
    return result


def select_schemas(
    cases: list[dict[str, Any]],
    full_schema: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, str]],
) -> tuple[dict[str, list[dict[str, list[dict[str, Any]]]]], dict[str, Any]]:
    q0 = [{table: full_schema[table] for table in case["gold_tables"]} for case in cases]
    bm25 = SchemaRetriever(top_k=TOP_K)
    q1 = []
    for case in cases:
        names = [item["table"] for item in bm25.search(case["question"], full_schema, relationships)[:TOP_K]]
        q1.append({name: full_schema[name] for name in names})

    from sentence_transformers import SentenceTransformer

    tables = sorted(full_schema)
    documents = [document_text(table, full_schema[table], relationships) for table in tables]
    started = perf_counter()
    model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
    load_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    document_vectors = model.encode(documents, normalize_embeddings=True, batch_size=4, show_progress_bar=False)
    query_vectors = model.encode(
        [case["question"] for case in cases], normalize_embeddings=True, batch_size=8, show_progress_bar=False
    )
    encode_ms = (perf_counter() - started) * 1000
    q2 = []
    for vector in query_vectors:
        scores = np.asarray(document_vectors) @ np.asarray(vector)
        names = [name for name, _ in sorted(zip(tables, scores), key=lambda item: (-float(item[1]), item[0]))[:TOP_K]]
        q2.append({name: full_schema[name] for name in names})
    return {"Q0_gold_schema": q0, "Q1_bm25": q1, "Q2_bge_m3": q2}, {
        "embedding_model": EMBEDDING_MODEL, "embedding_load_ms": round(load_ms, 3),
        "embedding_encode_ms": round(encode_ms, 3), "top_k": TOP_K,
    }


async def run_case(
    client: RealLLMClient,
    cache: ResultCache,
    runtime: SQLRuntime,
    case: dict[str, Any],
    split: str,
    method: str,
    selected_schema: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, str]],
    expected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rel = related(relationships, set(selected_schema))
    text = prompt(case, selected_schema, rel)
    config = {
        "stage": "text2sql", "method": method, "model": MODEL,
        "prompt_version": PROMPT_VERSION, "enable_thinking": ENABLE_THINKING,
        "max_tokens": MAX_TOKENS, "top_k": TOP_K, "fixture_version": FIXTURE_VERSION,
    }
    key = cache.key(case_id=case["case_id"], stage="text2sql", method=method, config=config, prompt=text)
    cached = cache.get(key)
    if cached is not None:
        return cached
    row = empty_record(case["case_id"], split, "text2sql", method, {
        "gold_sql": case["gold_sql"], "expected_result_signature": case["expected_result_signature"]
    })
    row.update(candidate_count=len(selected_schema), retrieved_tables=list(selected_schema), repair_attempted=False)
    started = perf_counter()
    try:
        candidate, telemetry = await client.structured(SQLCandidate, text)
        checked = await asyncio.to_thread(
            evaluate_candidate, runtime, candidate, set(selected_schema), expected_rows, case["expected_result_signature"]
        )
        row.update(
            success=checked["result_match"], prediction=candidate.model_dump(),
            error_type=None if checked["result_match"] else checked.get("runtime_error_type", "result_mismatch"),
            **checked, **telemetry,
        )
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc), api_calls=1)
    row["total_latency_ms"] = round((perf_counter() - started) * 1000, 3)
    validate_record(row)
    cache.put(key, row)
    return row


async def repair_case(
    client: RealLLMClient,
    cache: ResultCache,
    runtime: SQLRuntime,
    case: dict[str, Any],
    split: str,
    source: dict[str, Any],
    selected_schema: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, str]],
    expected_rows: list[dict[str, Any]],
    source_method: str,
) -> dict[str, Any]:
    rel = related(relationships, set(selected_schema))
    previous = source.get("prediction") or {"sql": "", "params": {}, "reason": "generation failed"}
    feedback = source.get("checker_feedback") or source.get("error") or source.get("error_type") or "unknown failure"
    text = repair_prompt(case, previous, feedback, selected_schema, rel)
    method = f"Q3_repair_{source_method.lower()}_failures"
    config = {
        "stage": "text2sql", "method": method, "model": MODEL,
        "prompt_version": PROMPT_VERSION, "enable_thinking": ENABLE_THINKING,
        "max_tokens": MAX_TOKENS, "top_k": TOP_K, "repair_limit": 1,
        "fixture_version": FIXTURE_VERSION,
    }
    key = cache.key(case_id=case["case_id"], stage="text2sql", method=method, config=config, prompt=text)
    cached = cache.get(key)
    if cached is not None:
        return cached
    row = empty_record(case["case_id"], split, "text2sql", method, source["gold"])
    row.update(candidate_count=len(selected_schema), retrieved_tables=list(selected_schema), repair_attempted=True)
    started = perf_counter()
    try:
        candidate, telemetry = await client.structured(SQLCandidate, text)
        checked = await asyncio.to_thread(
            evaluate_candidate, runtime, candidate, set(selected_schema), expected_rows, case["expected_result_signature"]
        )
        row.update(
            success=checked["result_match"], prediction=candidate.model_dump(), repair_success=checked["result_match"],
            error_type=None if checked["result_match"] else checked.get("runtime_error_type", "repair_result_mismatch"),
            **checked, **telemetry,
        )
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc), api_calls=1, repair_success=False)
    row["total_latency_ms"] = round((perf_counter() - started) * 1000, 3)
    validate_record(row)
    cache.put(key, row)
    return row


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = telemetry_summary(rows)
    for output, field in {
        "valid_sql_rate": "valid_sql", "execution_accuracy": "execution_success",
        "result_accuracy": "result_match", "security_pass_rate": "security_pass",
    }.items():
        result[output] = mean(bool(row.get(field)) for row in rows) if rows else 0.0
    result["schema_gold_table_recall"] = mean(
        len(set(row["retrieved_tables"]) & set(case["gold_tables"])) / len(set(case["gold_tables"]))
        for row, case in zip(rows, [case for case in split_cases("text2sql", rows[0]["split"])] if rows else [], strict=True)
    ) if rows else 0.0
    return result


async def main_async(
    split: str, methods: list[str], concurrency: int, database_url: str, repair_source: str | None,
) -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not (api_base and api_key and model_name):
        raise RuntimeError("real LLM environment is not configured")
    cases = split_cases("text2sql", split)
    inspector = PostgresSchemaInspector(PostgresDatasource("research", database_url))
    full_schema = inspector.tables()
    relationships = inspector.relationships()
    runtime = SQLRuntime(database_url, full_schema)
    expected_rows = await asyncio.gather(*(asyncio.to_thread(runtime.gold, case["gold_sql"]) for case in cases))
    selected, retrieval_telemetry = await asyncio.to_thread(select_schemas, cases, full_schema, relationships)
    cache = ResultCache()
    client = RealLLMClient(
        api_base=api_base, api_key=api_key, model=model_name, concurrency=concurrency,
        enable_thinking=ENABLE_THINKING, max_tokens=MAX_TOKENS,
    )
    results: dict[str, list[dict[str, Any]]] = {}
    try:
        for method in methods:
            rows = await asyncio.gather(*(
                run_case(client, cache, runtime, case, split, method, schema, relationships, gold_rows)
                for case, schema, gold_rows in zip(cases, selected[method], expected_rows, strict=True)
            ))
            results[method] = rows
            result = metrics(rows)
            config = {
                "stage": "text2sql", "split": split, "method": method,
                "model": MODEL, "prompt_version": PROMPT_VERSION, "concurrency": concurrency,
                "enable_thinking": ENABLE_THINKING, "max_tokens": MAX_TOKENS,
                "fixture_version": FIXTURE_VERSION,
                **retrieval_telemetry,
            }
            create_run(f"text2sql_{split}_{method.lower()}_output_contract_v2", config, rows, result)
            print(method, json.dumps(result, ensure_ascii=False), flush=True)

        if repair_source and repair_source in results:
            failed = [(index, row) for index, row in enumerate(results[repair_source]) if not row["success"]]
            repaired = await asyncio.gather(*(
                repair_case(
                    client, cache, runtime, cases[index], split, row, selected[repair_source][index],
                    relationships, expected_rows[index], repair_source,
                )
                for index, row in failed
            ))
            repair_method = f"Q3_repair_{repair_source.lower()}_failures"
            repair_metrics = telemetry_summary(repaired)
            repair_metrics.update({
                "attempted_failures": len(repaired),
                "repair_success_rate": mean(bool(row.get("repair_success")) for row in repaired) if repaired else 0.0,
                "source_method": repair_source,
                "initial_result_accuracy": metrics(results[repair_source])["result_accuracy"],
                "final_result_accuracy": (
                    sum(bool(row["success"]) for row in results[repair_source])
                    + sum(bool(row.get("repair_success")) for row in repaired)
                ) / len(cases),
            })
            config = {
                "stage": "text2sql", "split": split, "method": repair_method,
                "model": MODEL, "prompt_version": PROMPT_VERSION, "repair_limit": 1,
                "enable_thinking": ENABLE_THINKING, "max_tokens": MAX_TOKENS,
                "fixture_version": FIXTURE_VERSION,
                **retrieval_telemetry,
            }
            create_run(f"text2sql_{split}_{repair_method.lower()}_output_contract_v2", config, repaired, repair_metrics)
            print(repair_method, json.dumps(repair_metrics, ensure_ascii=False), flush=True)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--methods", nargs="+", default=["Q0_gold_schema", "Q1_bm25", "Q2_bge_m3"])
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--repair-source", choices=["Q0_gold_schema", "Q1_bm25", "Q2_bge_m3"], default="Q2_bge_m3")
    args = parser.parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    asyncio.run(main_async(args.split, args.methods, args.concurrency, database_url, args.repair_source))


if __name__ == "__main__":
    main()
