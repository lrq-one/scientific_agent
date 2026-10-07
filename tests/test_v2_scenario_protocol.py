from pathlib import Path

from evaluation.v2.validate_scenarios import validate


def test_v2_draft_splits_are_template_disjoint_and_not_claimed_frozen():
    path = Path(__file__).resolve().parents[1] / "evaluation" / "v2" / "scenario_cases.jsonl"
    result = validate(path)
    assert result["dev_cases"] == 15
    assert result["test_draft_cases"] == 15
    assert result["families_per_split"] == 15
    assert result["template_overlap"] == 0
    assert result["exact_prompt_overlap"] == 0
    assert result["independent_test_frozen"] is False
