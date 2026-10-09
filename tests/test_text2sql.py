import pytest

from app.models.schemas import SQLCandidate
from app.services.text2sql import SchemaRetriever, TextToSQLService
from app.tools.sql_guard import SQLGuard


SCHEMA = {
    "molecules": [{"name": "molecule_id", "type": "text"}, {"name": "structure_type", "type": "text"}],
    "training_molecules": [{"name": "molecule_id", "type": "text"}, {"name": "dataset_version", "type": "text"}],
}
RELATIONSHIPS = [{"source_table": "training_molecules", "source_column": "molecule_id", "target_table": "molecules", "target_column": "molecule_id"}]


class FakeSQLModel:
    def __init__(self):
        self.prompt = None

    def with_structured_output(self, schema):
        outer = self

        class Runnable:
            async def ainvoke(self, prompt, config=None):
                outer.prompt = prompt
                return SQLCandidate(
                    sql="SELECT molecule_id FROM training_molecules WHERE dataset_version = %(dataset_version)s",
                    params={"dataset_version": "%(dataset_version)s"},
                    reason="fake structured model",
                )
        return Runnable()


def test_bm25_schema_retrieval():
    relevant = SchemaRetriever().retrieve("training dataset_version coverage", SCHEMA)
    assert "training_molecules" in relevant


def test_coverage_prompt_preserves_absent_members_and_original_outcome():
    prompt = TextToSQLService().prompt("coverage", "sql", "training_db", SCHEMA, RELATIONSHIPS)
    assert "LEFT JOIN" in prompt and "non-members and zero-count groups" in prompt
    assert "observation-selected subgroup" in prompt


def test_schema_search_keeps_authorized_fk_dimension_and_exact_repair_feedback(monkeypatch):
    from app.tools.database_tools import DatabaseService
    from app.models.schemas import ToolResult
    service = object.__new__(DatabaseService)
    service.datasource_id = "training_db"
    service.schema = lambda: ToolResult(success=True, data=SCHEMA)
    service.relationships = lambda: ToolResult(success=True, data=RELATIONSHIPS)
    monkeypatch.setattr(SchemaRetriever, "search", lambda *args: [{"table": "training_molecules", "columns": SCHEMA["training_molecules"], "score": 1}])
    result = service.search_schema("training coverage", limit=1)
    assert {hit["table"] for hit in result.data} == set(SCHEMA)
    assert result.metadata["relationship_expanded_tables"] == ["molecules"]
    feedback = "grouping collapses original categories\nOriginal SQL: " + "x" * 350 + " FROM molecular_features"
    prompt = TextToSQLService().prompt("统计不同结构类型", "sql-repair", "training_db", SCHEMA, RELATIONSHIPS, repair_feedback=feedback)
    assert feedback in prompt
    assert "do not invent a different error" in prompt


@pytest.mark.asyncio
async def test_text2sql_fallback_is_parameterized(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Explicit fixture fallback only; never spend the user's live-provider tokens.
    monkeypatch.setattr("app.services.text2sql.ALLOW_DETERMINISTIC_LLM_FALLBACK", True)
    monkeypatch.setattr(TextToSQLService, "_configured_llm", lambda self: None)
    candidate, metadata = await TextToSQLService().generate(
        "检查 fused-ring 覆盖", "training-coverage", "training_db", SCHEMA, RELATIONSHIPS, "train_v3"
    )
    assert candidate.params == {"dataset_version": "train_v3"}
    assert "%(dataset_version)s" in candidate.sql
    assert metadata["generator"] == "deterministic_fixture_fallback"
    SQLGuard().validate(candidate.sql, set(SCHEMA), dialect="postgres")


@pytest.mark.asyncio
async def test_real_llm_integration_path_uses_structured_output():
    model = FakeSQLModel()
    candidate, metadata = await TextToSQLService(model).generate(
        "列出分子", "query", "training_db", SCHEMA, RELATIONSHIPS, "train_v3"
    )
    assert candidate.reason == "fake structured model"
    assert candidate.params["dataset_version"] == "train_v3"
    assert metadata["parameter_binding_repaired"] is True
    assert metadata["generator"] == "llm_structured_output"
    assert "Dataset version: train_v3" in model.prompt
    assert "%(dataset_version)s" in model.prompt
