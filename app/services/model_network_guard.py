"""Process-level network guard for offline and isolated test profiles.

The application has several provider construction paths (Text2SQL, planning,
skill routing and response generation).  Clearing environment variables is not
enough because a caller can construct a provider directly, or inherit a key
from the Windows environment.  This module guards the real httpcore transport
before a socket is opened while leaving in-process ASGI/Mock transports and
the explicitly isolated MinIO endpoints usable.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from threading import Lock
from typing import Any
from urllib.parse import urlparse


ACTIVE_PROFILES = {"offline", "integration_isolated"}
_ALLOWED_LOCAL_PORTS = {55433, 9002, 9003}
_MODEL_PATH_MARKERS = ("/chat/completions", "/completions", "/responses", "/embeddings")
_LOCK = Lock()
_INSTALLED = False
_BLOCKED: list[dict[str, Any]] = []


class ModelNetworkBlockedError(RuntimeError):
    """Raised before a real provider network request can open a socket."""


def _profile() -> str:
    return os.getenv("SCIENTIFIC_AGENT_TEST_PROFILE", "").strip().lower()


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore")
    return str(value or "")


def _request_parts(request: Any) -> tuple[str, int | None, str, dict[str, str]]:
    url = getattr(request, "url", None)
    host = _decode(getattr(url, "host", "")).lower()
    port = getattr(url, "port", None)
    path = _decode(getattr(url, "target", None) or getattr(url, "path", ""))
    headers = {}
    raw_headers = getattr(getattr(request, "headers", None), "raw", None)
    for item in raw_headers or getattr(request, "headers", []) or []:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        key, value = item[0], item[1]
        headers[_decode(key).lower()] = _decode(value)
    return host, port, path, headers


def _configured_model_hosts() -> set[str]:
    hosts: set[str] = {"api.openai.com", "dashscope.aliyuncs.com"}
    for name in ("LLM_API_BASE", "OPENAI_API_BASE"):
        raw = os.getenv(name)
        if raw:
            try:
                host = urlparse(raw).hostname
            except ValueError:
                host = None
            if host:
                hosts.add(host.lower())
    return hosts


def _is_in_process_transport(self: Any) -> bool:
    transport = getattr(self, "_network_backend", None) or getattr(self, "_transport", None)
    name = type(transport).__name__.lower()
    module = type(transport).__module__.lower()
    return any(marker in name or marker in module for marker in ("mock", "asgi", "wsgi", "testclient"))


def _check_request(self: Any, request: Any) -> None:
    profile = _profile()
    if profile not in ACTIVE_PROFILES or _is_in_process_transport(self):
        return

    host, port, path, headers = _request_parts(request)
    local = host in {"127.0.0.1", "localhost", "::1"}
    model_path = any(marker in path.lower() for marker in _MODEL_PATH_MARKERS)
    model_host = host in _configured_model_hosts()
    has_model_auth = any(key in headers for key in ("authorization", "api-key", "x-api-key"))
    isolated_service = local and port in _ALLOWED_LOCAL_PORTS and not model_path

    # The test profiles are intentionally deny-by-default for real HTTP.  The
    # only network exception is the explicitly named isolated MinIO service.
    if isolated_service:
        return
    reason = "model_endpoint" if model_path or model_host or has_model_auth else "external_network"
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "reason": reason,
        "host": host,
        "port": port,
        "path": path[:200],
    }
    with _LOCK:
        _BLOCKED.append(event)
    raise ModelNetworkBlockedError(
        f"real model/external network blocked in {profile} profile ({reason}); "
        "use Fake providers or explicitly select live_llm_manual"
    )


def install_model_network_guard() -> None:
    """Install the guard once; the active profile is read for every request."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        import importlib

        for core_name in ("httpcore", "httpcore2"):
            try:
                core = importlib.import_module(core_name)
            except ImportError:
                continue
            async_classes = [
                getattr(core, "AsyncConnectionPool", None),
                getattr(core, "AsyncHTTPProxy", None),
            ]
            sync_classes = [
                getattr(core, "ConnectionPool", None),
                getattr(core, "HTTPProxy", None),
            ]
            for cls in [item for item in async_classes if item is not None]:
                original = cls.handle_async_request

                async def guarded_async(self, request, _original=original):
                    _check_request(self, request)
                    return await _original(self, request)

                cls.handle_async_request = guarded_async
            for cls in [item for item in sync_classes if item is not None]:
                original = cls.handle_request

                def guarded_sync(self, request, _original=original):
                    _check_request(self, request)
                    return _original(self, request)

                cls.handle_request = guarded_sync
        # OpenAI-compatible clients may route through an HTTP proxy class that
        # is not reached through the pool class exported by this httpcore
        # version.  Guard the httpx boundary as well; MockTransport/ASGITransport
        # are explicitly exempted by _is_in_process_transport.
        for httpx_name in ("httpx", "httpx2"):
            try:
                httpx = importlib.import_module(httpx_name)
            except ImportError:
                continue
            async_send = httpx.AsyncClient.send
            sync_send = httpx.Client.send

            async def guarded_httpx_async(self, request, *args, _original=async_send, **kwargs):
                if not _is_in_process_transport(self):
                    _check_request(self, request)
                return await _original(self, request, *args, **kwargs)

            def guarded_httpx_sync(self, request, *args, _original=sync_send, **kwargs):
                if not _is_in_process_transport(self):
                    _check_request(self, request)
                return _original(self, request, *args, **kwargs)

            httpx.AsyncClient.send = guarded_httpx_async
            httpx.Client.send = guarded_httpx_sync
        _INSTALLED = True


def blocked_model_network_events() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(item) for item in _BLOCKED]


def reset_model_network_events() -> None:
    with _LOCK:
        _BLOCKED.clear()
