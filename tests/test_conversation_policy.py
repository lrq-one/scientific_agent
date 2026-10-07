from __future__ import annotations

from app.models.schemas import ResourceSummary
from app.services.conversation_policy import ConversationPolicy


RESOURCES = ResourceSummary(
    available_files=["model.csv"],
    authorized_datasources=["training_db"],
    available_mcp_tools=["get_molecule_features"],
)


def test_policy_handles_non_execution_language_without_tools():
    policy = ConversationPolicy()
    greeting = policy.deterministic("你好", has_context=False, active_task=False, resources=RESOURCES)
    assert greeting.interaction_type == "DIRECT_ANSWER"

    capability = policy.deterministic("你能做什么？", has_context=False, active_task=False, resources=RESOURCES)
    assert capability.interaction_type == "CAPABILITY_QUESTION"
    assert "PostgreSQL" in capability.direct_answer

    unsupported = policy.deterministic("明天天气怎么样？", has_context=False, active_task=False, resources=RESOURCES)
    assert unsupported.interaction_type == "UNSUPPORTED"

    vague = policy.deterministic("帮我分析一下", has_context=False, active_task=False, resources=RESOURCES)
    assert vague.interaction_type == "CLARIFY"


def test_policy_delegates_scientific_work_and_contextual_followup():
    policy = ConversationPolicy()
    task = policy.deterministic(
        "统计 training_db 中 train_v3 的 fused-ring 覆盖",
        has_context=False,
        active_task=False,
        resources=RESOURCES,
    )
    assert task.interaction_type == "DELEGATE"

    followup = policy.deterministic(
        "刚才这个结论的证据呢？",
        has_context=True,
        active_task=False,
        resources=RESOURCES,
    )
    assert followup.interaction_type == "DELEGATE"


def test_policy_cancel_only_targets_an_active_task():
    policy = ConversationPolicy()
    cancel = policy.deterministic("算了，不分析了", has_context=True, active_task=True, resources=RESOURCES)
    assert cancel.interaction_type == "CANCEL_ACTIVE"

    no_active = policy.deterministic("算了，不分析了", has_context=True, active_task=False, resources=RESOURCES)
    assert no_active is None or no_active.interaction_type != "CANCEL_ACTIVE"
