from pathlib import Path

from app.models.schemas import UserProfile
from app.services.prompt_catalog import PromptCatalog
from app.services.query_decomposition import analyze_training_error_coverage
from app.services.skills import SkillService
from app.services.user_profile import task_profile_context, validate_memory_write


def test_skill_catalog_has_derived_progressive_metadata_without_instructions():
    service = SkillService()
    catalog = service.catalog()
    assert len(catalog) >= 10
    assert {"id", "version", "content_hash", "enabled", "deprecated"} <= set(catalog[0])
    assert all("instructions" not in item for item in catalog)
    assert service.detail("training_coverage_analysis")["instructions"]


def test_prompt_catalog_is_hashable_and_node_specific():
    catalog = PromptCatalog()
    assert "GoalContract" in catalog.core()
    assert "authorized catalog" in catalog.fragment("skill_router")
    assert catalog.manifest_hash()


def test_user_profile_does_not_turn_task_facts_into_memory():
    profile = UserProfile(user_id="u", domain="mass_spec", default_units={"rt": "min"})
    assert task_profile_context(profile) == {
        "locale": "zh-CN", "response_detail": "standard", "domain": "mass_spec", "default_units": {"rt": "min"}
    }
    assert validate_memory_write("domain", explicit_user_confirmation=True)
    assert not validate_memory_write("dataset_version", explicit_user_confirmation=True, is_task_fact=True)
    assert not validate_memory_write("domain", explicit_user_confirmation=False)


def test_query_decomposition_requires_both_lineage_paths():
    schema = {name: [] for name in ("predictions", "model_runs", "experiments", "dataset_versions", "training_memberships")}
    relationships = [
        {"source_table": "predictions", "target_table": "model_runs"},
        {"source_table": "model_runs", "target_table": "experiments"},
        {"source_table": "experiments", "target_table": "dataset_versions"},
        {"source_table": "training_memberships", "target_table": "dataset_versions"},
    ]
    result = analyze_training_error_coverage(goal="比较 prediction 误差和训练覆盖", schema=schema, relationships=relationships)
    assert result["recommended_independent_populations"] == ["prediction_error", "training_coverage"]
    assert result["lineage_verified"] is True
    result = analyze_training_error_coverage(goal="比较 prediction 误差和训练覆盖", schema=schema, relationships=relationships[:-1])
    assert result["lineage_verified"] is False
