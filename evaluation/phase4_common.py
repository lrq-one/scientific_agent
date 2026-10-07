from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any

from evaluation.metrics import percentile


ROOT = Path(__file__).parent
RUNS_ROOT = ROOT / "runs"
CACHE_ROOT = ROOT / "cache"
SPLITS_ROOT = ROOT / "splits"
PROTOCOL_ROOT = ROOT / "protocol"
SEED = 20261006
MODEL = "qwen3.7-flash"
PROMPT_VERSION = "phase4-v1"
PRICE_CNY_PER_MILLION_INPUT = 0.2
PRICE_CNY_PER_MILLION_OUTPUT = 0.8


RECORD_FIELDS = (
    "case_id", "split", "stage", "method", "model", "prompt_version",
    "success", "prediction", "gold", "candidate_count", "input_tokens",
    "output_tokens", "total_tokens", "llm_latency_ms", "total_latency_ms",
    "fallback", "error_type", "cost_estimate",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")


def load_suite(stage: str) -> list[dict[str, Any]]:
    return load_jsonl(ROOT / stage / "cases.jsonl")


def split_cases(stage: str, split: str) -> list[dict[str, Any]]:
    manifest = json.loads((SPLITS_ROOT / f"{stage}.json").read_text(encoding="utf-8"))
    wanted = set(manifest[split])
    return [row for row in load_suite(stage) if row["case_id"] in wanted]


def cost_cny(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens * PRICE_CNY_PER_MILLION_INPUT / 1_000_000
        + output_tokens * PRICE_CNY_PER_MILLION_OUTPUT / 1_000_000,
        8,
    )


def empty_record(case_id: str, split: str, stage: str, method: str, gold: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "split": split,
        "stage": stage,
        "method": method,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "success": False,
        "prediction": None,
        "gold": gold,
        "candidate_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "llm_latency_ms": 0.0,
        "total_latency_ms": 0.0,
        "fallback": False,
        "error_type": None,
        "cost_estimate": 0.0,
    }


class ResultCache:
    def __init__(self, root: Path = CACHE_ROOT):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, *, case_id: str, stage: str, method: str, config: dict[str, Any], prompt: str) -> str:
        return sha256({
            "case_id": case_id,
            "stage": stage,
            "method": method,
            "config_hash": sha256(config),
            "prompt_version": PROMPT_VERSION,
            "model": MODEL,
            "prompt": prompt,
        })

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
        row["cache_hit"] = True
        return row

    def put(self, key: str, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["cache_hit"] = False
        write_json(self.root / f"{key}.json", payload)


def validate_record(row: dict[str, Any]) -> None:
    missing = set(RECORD_FIELDS) - set(row)
    if missing:
        raise ValueError(f"evaluation record missing fields: {sorted(missing)}")


def telemetry_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["total_latency_ms"]) for row in rows]
    llm_latencies = [float(row["llm_latency_ms"]) for row in rows]
    return {
        "cases": len(rows),
        "success_rate": mean(bool(row["success"]) for row in rows) if rows else 0.0,
        "fallback_rate": mean(bool(row["fallback"]) for row in rows) if rows else 0.0,
        "mean_latency_ms": mean(latencies) if rows else 0.0,
        "median_latency_ms": median(latencies) if rows else 0.0,
        "p95_latency_ms": percentile(latencies, 0.95),
        "mean_llm_latency_ms": mean(llm_latencies) if rows else 0.0,
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "total_tokens": sum(int(row["total_tokens"]) for row in rows),
        "mean_input_tokens": mean(int(row["input_tokens"]) for row in rows) if rows else 0.0,
        "mean_output_tokens": mean(int(row["output_tokens"]) for row in rows) if rows else 0.0,
        "mean_total_tokens": mean(int(row["total_tokens"]) for row in rows) if rows else 0.0,
        "cost_cny": round(sum(float(row["cost_estimate"]) for row in rows), 8),
        "api_calls": sum(int(row.get("api_calls", 0)) for row in rows),
        "cache_hits": sum(bool(row.get("cache_hit")) for row in rows),
    }


def create_run(experiment_id: str, config: dict[str, Any], rows: list[dict[str, Any]], metrics: dict[str, Any]) -> Path:
    run_dir = RUNS_ROOT / experiment_id
    errors = [row for row in rows if not row["success"]]
    write_json(run_dir / "config.json", config)
    write_jsonl(run_dir / "per_case.jsonl", rows)
    write_json(run_dir / "metrics.json", metrics)
    write_jsonl(run_dir / "errors.jsonl", errors)
    return run_dir


def configured_environment() -> dict[str, Any]:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    return {
        "api_base": api_base,
        "api_key_present": bool(api_key),
        "api_key_length": len(api_key or ""),
        "model": model,
    }

