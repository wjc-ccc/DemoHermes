import pytest
from pydantic import ValidationError
from agent.types.eval.datasets import MemoryEvalDataset
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def _definition(evidence=("t1",), active=()):
    return dict(dataset_id="perception_behavior", version="1.0.0", schema_version="2",
                title="时序", description="问题只读取历史前缀", created_at="2026-10-10T09:00:00+08:00",
                cases=[dict(case_id="prefix", title="前缀", scenario="correction", target="perception",
                            difficulty="multi_session", abilities=["temporal"],
                            history=[dict(session_id="s1", occurred_at="2026-10-01T09:00:00+08:00", turns=[
                                dict(turn_id="t1", user_text="保留期七天。"),
                                dict(turn_id="t2", user_text="先检查索引。", tool_results=[dict(
                                    tool_call_id="tool-1", name="read_file", arguments={"path":"index.txt"},
                                    status="error", error="missing index")]),
                                dict(turn_id="t3", user_text="更正为三十天。")])],
                            questions=[dict(question_id="q1", question_schema_version="2",
                                            history_end_turn_id="t2", expected_active_memory_ids=active,
                                            evidence_turn_ids=evidence, text="当前保留几天？",
                                            asked_at="2026-10-02T09:00:00+08:00",
                                            reference_answer="七天，索引检查失败。", required_meaning=["七天"])])])


def test_question_reads_only_its_declared_turn_prefix_and_versions_roundtrip(tmp_path):
    definition = MemoryEvalDataset.model_validate(_definition())
    case = definition.cases[0]
    history = case.history_for(case.questions[0])
    assert [turn.turn_id for session in history for turn in session.turns] == ["t1", "t2"]
    store = RuntimeStorage.open(tmp_path / "agent.db").evals
    store.publish(definition)
    assert store.get_dataset("perception_behavior", "1.0.0") == definition
    with store.db.read() as conn:
        assert conn.execute("SELECT question_schema_version,history_end_turn_id FROM eval_questions").fetchone() == ("2", "t2")


def test_question_cannot_reference_later_turn_or_an_unavailable_memory():
    with pytest.raises(ValidationError, match="未来"):
        MemoryEvalDataset.model_validate(_definition(evidence=("t3",)))
    with pytest.raises(ValidationError):
        MemoryEvalDataset.model_validate(_definition(active=("missing",)))


def test_results_are_bound_to_published_question_hash_and_answer_cannot_be_rewritten(tmp_path):
    definition = MemoryEvalDataset.model_validate(_definition())
    store = RuntimeStorage.open(tmp_path / "agent.db").evals
    store.publish(definition)
    store.start_run(run_id="r1", dataset_id=definition.dataset_id, dataset_version=definition.version,
                    provider="fixture", model="fixture-v1", prompt_version="repair-v1", extractor_version="v7")
    store.put_result("r1", "prefix", "q1", status="pending", result={"actual_answer":"七天"})
    store.put_result("r1", "prefix", "q1", status="passed", result={"actual_answer":"七天", "review":"evidence checked"})
    with pytest.raises(ValueError, match="rewritten"):
        store.put_result("r1", "prefix", "q1", status="passed", result={"actual_answer":"三十天"})
    with pytest.raises(ValueError, match="published dataset"):
        store.put_result("r1", "prefix", "missing", status="passed", result={"actual_answer":"七天"})
    record = store.get_run("r1")["results"][0]
    assert record["question_hash"] == definition.cases[0].questions[0].fingerprint()
    assert record["question_schema_version"] == "2"
    assert len(record["result"]["review_history"]) == 1


def test_full_history_baseline_includes_prior_session_without_future_or_source_rewrite(tmp_path):
    import json
    from agent.eval.long_dialogue import run_question
    from agent.types.model import ModelResponse
    body = _definition()
    turns = body["cases"][0]["history"][0]["turns"]
    body["cases"][0]["history"] = [
        dict(session_id="s1", occurred_at="2026-10-01T09:00:00+08:00", turns=turns[:1]),
        dict(session_id="s2", occurred_at="2026-10-02T09:00:00+08:00", turns=turns[1:]),
    ]
    body["cases"][0]["questions"][0]["asked_at"] = "2026-10-03T09:00:00+08:00"
    case = MemoryEvalDataset.model_validate(body).cases[0]
    class Model:
        name = "fixture"
        model_name = "baseline-coverage"
        def chat(self, messages, tools=None, **kwargs):
            if "评测审查员" in messages[0]["content"]:
                actual = messages[-1]["content"].split("## 实际回答\n", 1)[1].split("## 本检查点", 1)[0]
                return ModelResponse(text=json.dumps(dict(answer_passed="七天" in actual, memory_matches=[],
                    forbidden_card_ids=[], extra_card_ids=[], violations=[], reason="核对实际回答")))
            text = "\n".join(str(message.get("content", "")) for message in messages)
            assert "更正为三十天" not in text and "## 参考处理" not in text
            return ModelResponse(text="七天，索引检查失败。" if "保留期七天" in text else "保留期信息不足，索引检查失败。")
    result = run_question(case, case.questions[0], tmp_path, Model())
    first = result["baseline"]["trace"]["builds"][0]["messages"]
    assert "保留期七天" in str(first)
    assert "更正为三十天" not in str(first)
    path = tmp_path / "prefix__q1" / "baseline.db"
    with RuntimeStorage.open(path).db.read() as conn:
        original = conn.execute("SELECT content_text FROM messages WHERE message_id=?",
                                (result["source_message_id_map"]["t1"],)).fetchone()
        assert original == ("保留期七天。",)
