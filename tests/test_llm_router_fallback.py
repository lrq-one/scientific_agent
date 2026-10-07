import pytest

from app.agents.request_router import RequestRouter
from app.models.schemas import Capability, RequestIntent, ResourceSummary


class FakeStructuredLLM:
    def __init__(self, result):
        self.result = result
        self.called = False

    def with_structured_output(self, schema):
        outer = self

        class Runnable:
            async def ainvoke(self, prompt):
                outer.called = True
                return outer.result

        return Runnable()


@pytest.mark.asyncio
async def test_explicit_mixed_resource_route_is_deterministic_without_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resources = ResourceSummary(available_files=["model_v1.csv"], authorized_datasources=["training_db"])
    result = await RequestRouter().route_async("综合分析 model_v1.csv 和 training_db", resources)
    assert result.task_type == "mixed_analysis"
    assert "explicit file+database resource constraint" in result.reason


@pytest.mark.asyncio
async def test_llm_router_filters_hallucinated_resources():
    fake = FakeStructuredLLM(
        RequestIntent(
            goal="ignored",
            task_type="mixed_analysis",
            complexity="complex",
            required_capabilities=[Capability.FILE, Capability.DATABASE, Capability.SCIENTIFIC_MODEL],
            need_planning=True,
            reason="semantic result",
        )
    )
    result = await RequestRouter(fake).route_async("综合判断这个科研问题", ResourceSummary(authorized_datasources=["training_db"]))
    assert fake.called is True
    assert result.required_capabilities == [Capability.DATABASE]
    assert "resource-filtered" in result.reason


@pytest.mark.asyncio
async def test_simple_file_keeps_deterministic_fast_path():
    fake = FakeStructuredLLM(RequestIntent(goal="wrong"))
    result = await RequestRouter(fake).route_async("model_v1.csv 有多少行？", ResourceSummary(available_files=["model_v1.csv"]))
    assert result.task_type == "file_analysis"
    assert fake.called is False

