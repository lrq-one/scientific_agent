"""Development accounting regressions; no provider calls or DB execution."""
import pytest
from evaluation.p0_report import normalize_trials


def row(run, case, conversation):
    return {'run':run, 'case_id':case, 'conversation_id':conversation, 'scope_rows':[{'run':run}]}


def test_missing_case_supplement_preserves_source_but_uses_original_trial():
    results=normalize_trials([row('repeat2','D01','a'),row('repeat2_remaining','H01','b')])
    assert results[1]['run']=='repeat2'
    assert results[1]['source_phase']=='repeat2_remaining'
    assert results[1]['scope_rows'][0]['run']=='repeat2'


def test_supplement_cannot_replace_failed_original_outcome():
    with pytest.raises(ValueError,match='Duplicate logical trial'):
        normalize_trials([row('repeat2','D01','a'),row('repeat2_remaining','D01','b')])


def test_repeated_task_requires_new_conversation():
    with pytest.raises(ValueError,match='fresh conversation'):
        normalize_trials([row('repeat1','D01','a'),row('repeat2','D01','a')])


def test_same_task_can_be_observed_in_three_distinct_trials():
    results=normalize_trials([row('repeat1','D01','a'),row('repeat2','D01','b'),row('repeat3','D01','c')])
    assert len(results)==3
