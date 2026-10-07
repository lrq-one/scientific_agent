import pytest

from app.tools.registry import ToolChoice, ToolRegistry


def test_typed_tool_arguments_reject_stringified_containers_and_extra_fields():
    registry = ToolRegistry()
    valid = [
        ToolChoice(tool="compare_models", arguments={"filenames": ["a.csv", "b.csv"]}, reason="test"),
        ToolChoice(tool="save_result_table", arguments={"rows": [{"mae": 1.0}], "format": "csv"}, reason="test"),
        ToolChoice(tool="list_workspace_files", arguments={}, reason="test"),
    ]
    invalid = [
        ToolChoice(tool="compare_models", arguments={"filenames": "a.csv,b.csv"}, reason="test"),
        ToolChoice(tool="save_result_table", arguments={"rows": "[]", "format": "csv"}, reason="test"),
        ToolChoice(tool="list_workspace_files", arguments={"filename": None}, reason="test"),
        ToolChoice(tool="execute_readonly_sql", arguments={"sql": "SELECT 1", "role": "admin"}, reason="test"),
    ]
    assert all(registry.validate_choice(item)[0] for item in valid)
    assert all(not registry.validate_choice(item)[0] for item in invalid)


@pytest.mark.asyncio
async def test_invalid_llm_tool_arguments_are_not_accepted():
    class Structured:
        async def ainvoke(self, prompt, config=None):
            return ToolChoice(tool="compare_models", arguments={"filenames": "a.csv,b.csv"}, reason="bad")

    class FakeLLM:
        def with_structured_output(self, model):
            return Structured()

    registry = ToolRegistry(llm=FakeLLM())
    choices = [registry.specs["compare_models"]]
    choice, source, telemetry = await registry.select("compare two models", "compare", choices, "compare_models")
    assert source == "llm_invalid_or_unauthorized_arguments"
    assert choice is None
    assert telemetry["llm_called"] is True
