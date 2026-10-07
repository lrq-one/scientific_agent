from pathlib import Path

import pandas as pd
import pytest

from app.agents.scientific_agent import ScientificAgent
from app.config import DEMO_DATA
from app.tools.file_tools import FileAnalysisService


def test_model_comparison_aligns_by_molecule_and_exposes_subgroup_delta():
    result = FileAnalysisService().compare_models([DEMO_DATA / "model_v1.csv", DEMO_DATA / "model_v2.csv"])
    assert result.success is True
    assert result.data["comparable"] is True
    assert result.data["aligned_count"] == 8
    fused = next(row for row in result.data["subgroups"] if row["structure_type"] == "fused_ring")
    assert fused["count"] == 2
    assert fused["delta_right_minus_left"] > 0
    assert result.data["paired_mae_delta_right_minus_left"] > 0


def test_model_comparison_does_not_claim_pairwise_gain_for_unmatched_ids(tmp_path: Path):
    left = pd.read_csv(DEMO_DATA / "model_v1.csv")
    right = pd.read_csv(DEMO_DATA / "model_v2.csv").iloc[:-1]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    left.to_csv(a, index=False)
    right.to_csv(b, index=False)
    result = FileAnalysisService().compare_models([a, b])
    assert result.success is True
    assert result.data["comparable"] is False
    assert result.data["left_only_count"] == 1
    assert "paired_mae_delta_right_minus_left" not in result.data


def test_model_comparison_rejects_disagreeing_observed_rt(tmp_path: Path):
    left = pd.read_csv(DEMO_DATA / "model_v1.csv")
    right = pd.read_csv(DEMO_DATA / "model_v2.csv")
    right.loc[0, "observed_rt"] = 99.0
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    left.to_csv(a, index=False)
    right.to_csv(b, index=False)
    result = FileAnalysisService().compare_models([a, b])
    assert result.success is False
    assert "observed_rt differs" in result.error


@pytest.mark.asyncio
async def test_product_file_branch_executes_paired_comparison(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    events = [item async for item in ScientificAgent().stream(
        "比较 model_v1.csv 和 model_v2.csv 的结构误差", "tester", "paired-model-product"
    )]
    paired = next(item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "compare_models")
    assert paired.data["result"]["data"]["comparable"] is True
    assert any(item.event == "EVIDENCE_ADDED" and "配对" in item.data["evidence"]["claim"] for item in events)
    assert events[-1].data["state"]["quality_status"] == "SUPPORTED_CONCLUSION"
