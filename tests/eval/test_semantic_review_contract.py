import pytest
from agent.eval.long_dialogue import _validate_review, _memory_candidates, _expired_checkpoint_cards
from agent.eval.datasets.long_dialogues import load_long_datasets
from tests.core.memory.test_memory_extractor import _stored_card


def review(**changes):
    return dict(answer_passed=True,memory_matches=[],forbidden_card_ids=[],
                extra_card_ids=[],violations=[],reason="checked") | changes


def test_recall_requires_actual_card_not_answer_or_expected_identity():
    with pytest.raises(ValueError,match="real card"):
        _validate_review(review(memory_matches=[dict(memory_id="schedule",card_ids=["schedule"])]),
                         {"schedule":["actual"]},{"actual"},"schedule present in answer")
    with pytest.raises(ValueError,match="real card"):
        _validate_review(review(memory_matches=[dict(memory_id="absent",card_ids=["actual"])]),
                         {"schedule":["actual"]},{"actual"},"schedule present in answer")


def test_matching_standard_is_not_duplicate_card_and_empty_answer_never_passes():
    good=review(memory_matches=[dict(memory_id="schedule",card_ids=["actual"],reason="supported")])
    assert _validate_review(good,{"schedule":["actual"]},{"actual"},"Friday")["matched_memory_ids"]==["schedule"]
    with pytest.raises(ValueError,match="contradictory"):
        _validate_review(good | {"extra_card_ids":["actual"]},{"schedule":["actual"]},{"actual"},"Friday")
    with pytest.raises(ValueError,match="empty answer"):
        _validate_review(review(),{},set()," ")


def test_wrong_category_or_unrelated_source_cannot_match_even_if_text_similar():
    case=next(c for c in load_long_datasets()[0].cases if c.case_id=="profession_phone")
    question=case.questions[0]
    expected=next(e for e in case.expected_memories if e.memory_id in question.expected_active_memory_ids)
    source_map={identity:"actual-source" for identity in expected.evidence_turn_ids}
    card=_stored_card("actual",expected.claim,kind=expected.kind,slot_key="profile:user:profession")
    assert "actual" not in _memory_candidates(case,question,[card],source_map)[expected.memory_id]
    card=card.model_copy(update={"source_ref":"actual-source"})
    assert "actual" in _memory_candidates(case,question,[card],source_map)[expected.memory_id]
    card=card.model_copy(update={"kind":"ongoing_fact"})
    assert "actual" not in _memory_candidates(case,question,[card],source_map)[expected.memory_id]


def test_explicit_absence_is_not_credited_as_a_memory_match():
    absent=review(memory_matches=[dict(memory_id="schedule",card_ids=[],reason="not extracted")])
    assert _validate_review(absent,{"schedule":[]},set(),"Friday")["matched_memory_ids"]==[]


def test_expiry_metadata_is_checked_independently_of_semantic_answer():
    case=next(c for c in load_long_datasets()[0].cases if c.case_id=="capacity_expiry")
    expected=case.expected_memories[0]
    source_map={identity:"actual-source" for identity in expected.evidence_turn_ids}
    card=_stored_card("actual",expected.claim,kind=expected.kind,slot_key="ongoing_fact:user:study_capacity")
    card=card.model_copy(update={"source_ref":"actual-source"})
    assert _memory_candidates(case,case.questions[0],[card],source_map)[expected.memory_id]==[]
    assert _expired_checkpoint_cards(case,case.questions[1],[card],source_map)==["actual"]
    card=card.model_copy(update={"valid_until":expected.valid_until})
    assert _memory_candidates(case,case.questions[0],[card],source_map)[expected.memory_id]==["actual"]
