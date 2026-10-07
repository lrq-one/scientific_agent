from __future__ import annotations

import argparse
import asyncio
import json
import os
from statistics import mean
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from app.services.skills import SkillService
from app.services.text2sql import tokenize
from evaluation.metrics import skill_routing_metrics
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


class SkillRanking(BaseModel):
    ranked_skills: list[str] = Field(description="Candidate skill names ranked from most to least relevant")


class SkillRetriever:
    def __init__(self, skills: list[dict[str, Any]]):
        self.skills = skills
        documents = []
        for skill in skills:
            text = " ".join([
                skill["name"], skill["description"],
                *map(str, skill.get("tags", [])),
                *map(str, skill.get("domains", [])),
                *map(str, skill.get("intents", [])),
            ])
            documents.append(tokenize(text))
        self.bm25 = BM25Okapi(documents)

    def top(self, query: str, k: int) -> list[dict[str, Any]]:
        query_tokens = tokenize(query)
        scores = self.bm25.get_scores(query_tokens)
        ranked = sorted(zip(self.skills, scores), key=lambda item: (-float(item[1]), item[0]["name"]))
        return [item[0] for item in ranked[:k]]


def compact(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": skill["name"],
        "description": skill["description"],
        "tags": skill.get("tags", []),
        "intents": skill.get("intents", []),
        "required_capabilities": skill.get("required_capabilities", []),
    }


def prompt(case: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    return (
        "Rank the supplied scientific skills for the request. Use only candidate names. "
        "Return the relevant skills first; omit skills that are not relevant.\n"
        f"Request: {case['query']}\n"
        f"Context: {json.dumps(case['context'], ensure_ascii=False)}\n"
        f"Candidate skills: {json.dumps([compact(item) for item in candidates], ensure_ascii=False)}"
    )


async def run_case(
    client: RealLLMClient,
    cache: ResultCache,
    retriever: SkillRetriever,
    all_skills: list[dict[str, Any]],
    case: dict[str, Any],
    split: str,
    method: str,
    reported_method: str | None = None,
) -> dict[str, Any]:
    reported_method = reported_method or method
    retrieval_started = perf_counter()
    if method == "S0_all9":
        candidates = all_skills
    else:
        k = int(method.rsplit("k", 1)[1])
        candidates = retriever.top(case["query"], k)
    retrieval_ms = round((perf_counter() - retrieval_started) * 1000, 3)
    text = prompt(case, candidates)
    no_thinking = reported_method.endswith("_nothinking")
    config = {
        "stage": "skill_routing", "method": reported_method, "model": MODEL,
        "prompt_version": PROMPT_VERSION, "enable_thinking": not no_thinking,
        "max_tokens": 1024 if no_thinking else None,
    }
    key = cache.key(case_id=case["case_id"], stage="skill_routing", method=reported_method, config=config, prompt=text)
    cached = cache.get(key)
    if cached is not None:
        return cached
    expected = case["gold_skills"]
    candidate_names = [item["name"] for item in candidates]
    row = empty_record(case["case_id"], split, "skill_routing", reported_method, expected)
    row["candidate_count"] = len(candidates)
    row["retrieval_latency_ms"] = retrieval_ms
    try:
        result, telemetry = await client.structured(SkillRanking, text)
        ranked = []
        for name in result.ranked_skills:
            if name in candidate_names and name not in ranked:
                ranked.append(name)
        recall = len(set(expected) & set(ranked[:3])) / len(set(expected))
        success = recall == 1.0
        retrieval_recall = len(set(expected) & set(candidate_names)) / len(set(expected))
        row.update(
            success=success,
            prediction=ranked,
            error_type=None if success else "schema_retrieval_error" if retrieval_recall < 1 else "ranking_error",
            retrieval_recall=retrieval_recall,
            **telemetry,
        )
        row["total_latency_ms"] = round(retrieval_ms + telemetry["llm_latency_ms"], 3)
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc), api_calls=1, total_latency_ms=retrieval_ms)
    validate_record(row)
    cache.put(key, row)
    return row


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["prediction"] is not None]
    result = telemetry_summary(rows)
    if valid:
        result.update(skill_routing_metrics([row["gold"] for row in valid], [row["prediction"] for row in valid]))
    result["average_candidate_count"] = mean(row["candidate_count"] for row in rows) if rows else 0.0
    result["candidate_reduction_rate"] = 1 - result["average_candidate_count"] / 9 if rows else 0.0
    result["retrieval_recall"] = mean(float(row.get("retrieval_recall", 1.0)) for row in rows) if rows else 0.0
    return result


async def main_async(split: str, methods: list[str], concurrency: int) -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not (api_base and api_key and model):
        raise RuntimeError("real LLM environment is not configured")
    skills = SkillService().load()
    retriever = SkillRetriever(skills)
    cases = split_cases("skill_routing", split)
    cache = ResultCache()
    try:
        for requested_method in methods:
            no_thinking = requested_method.endswith("_nothinking")
            method = requested_method.removesuffix("_nothinking")
            client = RealLLMClient(
                api_base=api_base, api_key=api_key, model=model, concurrency=concurrency,
                enable_thinking=False if no_thinking else None,
                max_tokens=1024 if no_thinking else None,
            )
            rows = await asyncio.gather(*(
                run_case(client, cache, retriever, skills, case, split, method, requested_method) for case in cases
            ))
            await client.close()
            config = {
                "stage": "skill_routing", "split": split, "method": requested_method,
                "model": MODEL, "prompt_version": PROMPT_VERSION, "concurrency": concurrency,
                "enable_thinking": not no_thinking,
                "max_tokens": 1024 if no_thinking else None,
            }
            result = metrics(rows)
            create_run(f"skill_{split}_{requested_method.lower()}", config, rows, result)
            print(requested_method, result, flush=True)
    finally:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--methods", nargs="+", default=["S0_all9", "S1_bm25_k3", "S1_bm25_k5"])
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(main_async(args.split, args.methods, args.concurrency))


if __name__ == "__main__":
    main()
