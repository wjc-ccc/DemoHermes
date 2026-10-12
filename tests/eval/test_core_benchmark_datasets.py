"""冻结题库数量、长度、时序、独立性和旧版本指纹验证。"""
from collections import Counter
from agent.eval.datasets.core_benchmark import build_definitions
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def test_formal_120_cases_360_questions_have_balanced_lengths_and_families(tmp_path):
    definitions=build_definitions()
    assert [len(definition.cases) for definition in definitions]==[60,30,30]
    cases=[case for definition in definitions for case in definition.cases]
    assert Counter(sum(len(session.turns) for session in case.history) for case in cases)=={40:40,80:40,120:40}
    assert sum(len(case.questions) for case in cases)==360
    assert [len({case.scenario for case in definition.cases}) for definition in definitions]==[12,10,6]
    store=RuntimeStorage.open(tmp_path/"eval.db").evals
    for definition in definitions:
        assert definition.schema_version=="3"
        store.publish(definition)
        assert store.get_dataset(definition.dataset_id,definition.version)==definition
    for case in cases:
        n=sum(len(session.turns) for session in case.history)
        for question,end in zip(case.questions,(n//4,3*n//5,n)):
            prefix=case.history_for(question)
            assert sum(len(session.turns) for session in prefix)==end
            allowed={turn.turn_id for session in prefix for turn in session.turns}
            assert set(question.evidence_turn_ids)<=allowed
            selected=[memory for memory in case.expected_memories if memory.memory_id in question.expected_active_memory_ids]
            assert all(memory.valid_until is None or question.asked_at < memory.valid_until for memory in selected)


def test_pilot_has_20_independent_cases_covering_every_target():
    pilot=build_definitions(pilot=True)
    formal=build_definitions()
    assert [len(definition.cases) for definition in pilot]==[8,6,6]
    assert sum(len(case.questions) for definition in pilot for case in definition.cases)==60
    assert not {case.case_id for definition in pilot for case in definition.cases}&{case.case_id for definition in formal for case in definition.cases}


def test_published_long_dataset_hashes_match_perception_naming():
    assert [definition.fingerprint() for definition in load_long_datasets()]==[
        "0166e36038f30cc5586b3ae99c10f42f5eed0ad9978240272f3cb3607b5cb406",
        "ea2e721b50ac5bf812e2bb5f4f4ce7c5bd669019ef636ff7f598a0d432245e1a",
        "0041a80458245e5e0ec64b45a74988743a965a768f8c485135b471065c273bf9"]


def test_similar_perception_families_have_different_final_truth_not_just_names():
    definition=build_definitions()[0]
    answers=[next(case for case in definition.cases if case.scenario==family).questions[-1].reference_answer
             for family in ("distributed","synthesis","correction","anchor_growth")]
    assert len(set(answers))==4
