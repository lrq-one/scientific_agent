import pytest

from app.services.demo_data import ensure_demo_data
from app.models.schemas import FollowUpDecision


class StructuredDecisionStub:
    """Offline protocol stub, NOT a semantic classifier or evaluation result."""
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.prompts = []

    def with_structured_output(self, schema):
        assert schema["title"] == "FollowUpDecision"
        assert "source" not in schema["properties"] and "llm_telemetry" not in schema["properties"]
        return self

    async def ainvoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return self.decisions.pop(0).model_copy(deep=True) if self.decisions else FollowUpDecision(interaction_type="NEW_TASK")


@pytest.fixture(autouse=True)
def offline_followup_model(monkeypatch):
    # Existing integration tests must not spend tokens or pretend rule routing is LLM.
    from app.api import routes
    model = StructuredDecisionStub()
    monkeypatch.setattr(routes.followup_resolver, "llm", model)
    return model


@pytest.fixture(scope="session", autouse=True)
def demo_fixtures():
    ensure_demo_data()
