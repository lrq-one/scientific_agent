import pytest

from app.agents.request_router import RequestRouter
from app.models.schemas import Capability, RequestIntent, ResourceSummary


RESOURCES = ResourceSummary(
    available_files=["model_v1.csv", "model_v2.csv"],
    authorized_datasources=["training_db"],
)


def test_intent_router_structured_output():
    result = RequestRouter().route("model_v1.csv 有多少行？", RESOURCES)
    assert isinstance(result, RequestIntent)
    assert result.task_type == "file_analysis"
    assert result.complexity == "simple"
    assert result.required_capabilities == [Capability.FILE]


def test_simple_database_route():
    result = RequestRouter().route("training_db 中 fused-ring 有多少条？", RESOURCES)
    assert result.task_type == "database_analysis"
    assert result.complexity == "simple"
    assert Capability.DATABASE in result.required_capabilities


def test_mixed_route():
    query = "比较 model_v1.csv 和 model_v2.csv，分析为什么 fused-ring 误差更高，并检查 training_db 训练覆盖。"
    result = RequestRouter().route(query, RESOURCES)
    assert result.task_type == "mixed_analysis"
    assert result.complexity == "complex"
    assert result.need_planning is True
    assert result.required_capabilities == [Capability.FILE, Capability.DATABASE]


def test_unavailable_resource_not_added_as_capability():
    result = RequestRouter().route("查询 training_db", ResourceSummary())
    assert result.task_type == "database_analysis"
    assert result.required_capabilities == []


@pytest.mark.asyncio
async def test_explicit_file_database_constraint_does_not_ask_llm_to_drop_file():
    class WrongLLM:
        def with_structured_output(self, model):
            raise AssertionError("explicit mixed-resource request must use deterministic constraint")

    router = RequestRouter(llm=WrongLLM())
    intent = await router.route_async(
        "比较 model_v1.csv 和 model_v2.csv，并检查 training_db train_v3 训练覆盖", RESOURCES
    )
    assert intent.task_type == "mixed_analysis"
    assert intent.required_capabilities == [Capability.FILE, Capability.DATABASE]

