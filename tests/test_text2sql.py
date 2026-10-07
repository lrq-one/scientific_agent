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


@pytest.mark.asyncio
async def test_text2sql_fallback_is_parameterized(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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

