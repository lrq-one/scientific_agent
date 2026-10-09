import httpx
import pytest


@pytest.mark.asyncio
async def test_offline_profile_blocks_real_model_http_before_socket(monkeypatch):
    from app.services.model_network_guard import (
        ModelNetworkBlockedError,
        blocked_model_network_events,
        reset_model_network_events,
    )

    monkeypatch.setenv("SCIENTIFIC_AGENT_TEST_PROFILE", "offline")
    reset_model_network_events()
    with pytest.raises(ModelNetworkBlockedError, match="real model/external network blocked"):
        async with httpx.AsyncClient(base_url="http://127.0.0.1:9") as client:
            await client.post("/v1/chat/completions", json={"model": "qwen-test"})
    events = blocked_model_network_events()
    assert events and events[-1]["reason"] == "model_endpoint"
    assert events[-1]["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_in_process_fake_transport_is_not_blocked(monkeypatch):
    monkeypatch.setenv("SCIENTIFIC_AGENT_TEST_PROFILE", "integration_isolated")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"fake": True}))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        response = await client.post("/v1/chat/completions", json={"model": "fake"})
    assert response.status_code == 200
    assert response.json() == {"fake": True}


@pytest.mark.asyncio
async def test_direct_text2sql_provider_cannot_bypass_guard(monkeypatch):
    from app.services.model_network_guard import ModelNetworkBlockedError, blocked_model_network_events, reset_model_network_events
    from app.services.text2sql import TextToSQLService

    monkeypatch.setenv("SCIENTIFIC_AGENT_TEST_PROFILE", "offline")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_API_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    reset_model_network_events()
    service = TextToSQLService()
    with pytest.raises(RuntimeError, match="real Text-to-SQL LLM call failed") as error:
        await service.generate(
            "count molecules",
            "query",
            "isolated",
            {"molecules": [{"name": "molecule_id", "type": "uuid"}]},
            [],
        )
    assert isinstance(error.value.__cause__, ModelNetworkBlockedError)
    assert blocked_model_network_events()[-1]["reason"] == "model_endpoint"
