from __future__ import annotations

import os
from typing import Any

import psycopg

from app.services.llm_config import llm_settings


def _component(status: str, *, ready: bool, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "ready": ready}
    if detail:
        payload["detail"] = detail
    payload.update(extra)
    return payload


def check_database() -> dict[str, Any]:
    url = os.getenv("DATABASE_URL")
    if not url:
        return _component("missing", ready=False, detail="DATABASE_URL is not configured")
    try:
        with psycopg.connect(url, connect_timeout=2) as connection:
            value = connection.execute("SELECT 1").fetchone()[0]
        return _component("ok", ready=value == 1)
    except Exception as exc:
        return _component("unreachable", ready=False, detail=f"{type(exc).__name__}: {exc}")


def check_checkpoint(checkpointing) -> dict[str, Any]:
    configured = bool(os.getenv("CHECKPOINT_DATABASE_URL"))
    if not configured:
        return _component("memory", ready=False, detail="persistent checkpoint store is not configured")
    if not checkpointing.persistent:
        return _component("unavailable", ready=False, detail="configured checkpoint store did not initialize")
    try:
        connection = checkpointing.connection
        if connection is not None:
            row = connection.execute("SELECT 1 AS ok").fetchone()
            if row is None:
                return _component("unavailable", ready=False, detail="checkpoint database ping returned no row")
            # CheckpointService uses psycopg dict_row, so fetchone() returns a
            # mapping rather than a positional tuple.
            value = row.get("ok") if isinstance(row, dict) else row[0]
            if value != 1:
                return _component("unavailable", ready=False, detail="checkpoint database ping failed")
        return _component("ok", ready=True)
    except Exception as exc:
        return _component("unreachable", ready=False, detail=f"{type(exc).__name__}: {exc}")


def check_object_storage(storage) -> dict[str, Any]:
    if not storage.configured:
        return _component("missing", ready=False, detail="MinIO is not configured")
    try:
        exists = storage.client.bucket_exists(storage.bucket)
        if not exists:
            storage.ensure_bucket()
            exists = storage.client.bucket_exists(storage.bucket)
        return _component("ok" if exists else "unavailable", ready=bool(exists), bucket=storage.bucket)
    except Exception as exc:
        return _component("unreachable", ready=False, detail=f"{type(exc).__name__}: {exc}", bucket=storage.bucket)


def check_llm() -> dict[str, Any]:
    settings = llm_settings()
    if not settings.configured:
        return _component("missing", ready=False, detail="LLM_API_KEY/LLM_MODEL are not configured")
    # Readiness intentionally does not spend tokens. Real provider reachability is
    # verified by smoke/evaluation calls and surfaced separately in telemetry.
    return _component(
        "configured",
        ready=True,
        model=settings.model,
        api_base=settings.api_base,
        network_probe="not_performed",
    )


def runtime_readiness(*, storage, checkpointing) -> dict[str, Any]:
    components = {
        "database": check_database(),
        "object_storage": check_object_storage(storage),
        "checkpoint": check_checkpoint(checkpointing),
        "llm": check_llm(),
        "mcp": _component("on_demand", ready=True, transport="stdio"),
        "deepagents": _component("available", ready=True),
    }
    blocking = ("database", "checkpoint", "llm")
    ready = all(components[name]["ready"] for name in blocking)
    # MinIO is useful but should not block plain DB/chat tasks.
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "components": components,
    }
