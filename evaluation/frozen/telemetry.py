"""HTTP-boundary accounting of every provider attempt, including rejected JSON.

Only transport metadata and usage are retained. No keys/prompts/hidden reasoning.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
from threading import Lock
from time import perf_counter
import uuid

request_identity = ContextVar("evaluation_request_identity", default={})
_lock = Lock()


def install(output: Path, variant: str):
    import httpx
    import httpx2
    from app.services.execution_context import execution_identity
    output.parent.mkdir(parents=True, exist_ok=True)
    clients = [httpx, httpx2] if httpx2 is not httpx else [httpx]
    if any(getattr(module.AsyncClient, "_evaluation_accounted", False) for module in clients):
        raise RuntimeError("Telemetry already installed")

    def record(data):
        with _lock, output.open("a", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False) + "\n")

    def begin(request):
        try:
            body = json.loads(request.content)
        except Exception:
            body = {}
        data = {"attempt_id": str(uuid.uuid4()), "variant": variant,
                "started_at": datetime.now(timezone.utc).isoformat(),
                **request_identity.get(), **execution_identity.get(),
                "model": body.get("model"), "enable_thinking": body.get("enable_thinking"),
                "max_tokens": body.get("max_tokens", body.get("max_completion_tokens")), "temperature": body.get("temperature"),
                "request_body_sha256": hashlib.sha256(request.content).hexdigest(),
                "phase": "provider_started"}
        record(data)
        return data

    def finish(data, started, response=None, error=None):
        data = {**data, "phase": "provider_finished", "latency_ms": (perf_counter()-started)*1000,
                "http_status": response.status_code if response is not None else None,
                "error_type": type(error).__name__ if error else None}
        usage = None
        if response is not None and not response.is_closed and not response.is_stream_consumed:
            data["usage_status"] = "not_measured_stream"
        elif response is not None:
            try:
                body = response.json()
                usage = body.get("usage")
                data["actual_model"] = body.get("model")
                data["finish_reason"] = (body.get("choices") or [{}])[0].get("finish_reason")
            except Exception:
                pass
        data["usage"] = usage
        data.setdefault("usage_status", "measured" if usage else "not_reported")
        record(data)

    def wrap(original):
        async def send(client, request, *args, **kwargs):
            if not request.url.path.endswith("/chat/completions"):
                return await original(client, request, *args, **kwargs)
            data, started = begin(request), perf_counter()
            try:
                response = await original(client, request, *args, **kwargs)
            except BaseException as error:
                finish(data, started, error=error)
                raise
            finish(data, started, response=response)
            return response
        return send
    originals = []
    for module in clients:
        original = module.AsyncClient.send
        originals.append((module, original))
        module.AsyncClient.send = wrap(original)
        module.AsyncClient._evaluation_accounted = True
    def restore():
        for module, original in originals:
            module.AsyncClient.send = original
            module.AsyncClient._evaluation_accounted = False
    return restore


class IdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        parts = path.split("/")
        identity = {"request_id": str(uuid.uuid4()), "endpoint": path}
        if len(parts) > 3 and parts[2] == "conversations":
            identity["conversation_id"] = parts[3]
        token = request_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            request_identity.reset(token)
