"""Offline checks: declared Skill prerequisites constrain LLM selection."""
from __future__ import annotations

import pytest

from app.services.skills import SkillSelection, SkillService


class FakeModel:
    def __init__(self, selected):
        self.selected = selected
        self.requests = []

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, prompt, config=None):
        self.requests.append(prompt)
        return SkillSelection(selected_skills=self.selected)


@pytest.mark.asyncio
async def test_run_comparison_cannot_select_two_dataset_version_skill():
    llm = FakeModel(["cross_dataset_comparison", "experiment_run_diagnosis"])
    service = SkillService(llm=llm)
    selected, source, usage = await service.select_async(
        "用 training_db 的 predictions 比较 baseline-run 与 candidate-run 的分组 MAE，导出 CSV",
        "general", {"database", "artifact"},
    )
    assert source == "llm_structured"
    assert "cross_dataset_comparison" not in selected
    assert "experiment_run_diagnosis" in selected
    assert llm.requests
    assert "cross_dataset_comparison" not in llm.requests[0]


@pytest.mark.asyncio
async def test_explicit_two_training_versions_keep_specialized_skill_available():
    llm = FakeModel(["cross_dataset_comparison"])
    selected, _, _ = await SkillService(llm=llm).select_async(
        "比较 training_db 的 train_v2 和 train_v3 两个训练版本的结构分布",
        "general", {"database", "artifact"},
    )
    assert selected == ["cross_dataset_comparison"]
    assert "cross_dataset_comparison" in llm.requests[0]
