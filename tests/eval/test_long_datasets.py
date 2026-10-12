from agent.eval.datasets.long_dialogues import build_definitions, load_long_datasets
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def test_frozen_long_datasets_cover_all_48_scenarios_and_96_questions(tmp_path):
    definitions = load_long_datasets()
    assert [len(d.cases) for d in definitions] == [24, 16, 8]
    assert sum(len(c.questions) for d in definitions for c in d.cases) == 96
    assert [d.fingerprint() for d in definitions] == [d.fingerprint() for d in build_definitions()]
    assert sum(not c.expected_memories for c in definitions[0].cases) == 12
    statuses = {tool.status for d in definitions for c in d.cases for s in c.history for t in s.turns for tool in t.tool_results}
    assert {"success", "error", "timeout"} <= statuses
    store = RuntimeStorage.open(tmp_path / "agent.db").evals
    for definition in definitions:
        assert store.publish(definition)
        assert not store.publish(definition)
        assert store.get_dataset(definition.dataset_id, definition.version) == definition
        for case in definition.cases:
            assert 40 <= sum(len(s.turns) for s in case.history) <= 80
            for question in case.questions:
                allowed = {t.turn_id for s in case.history_for(question) for t in s.turns}
                assert set(question.evidence_turn_ids) <= allowed
                assert question.question_schema_version == "2"
                assert question.expected_active_memory_ids is not None
                assert len(question.evidence_turn_ids) >= 2
