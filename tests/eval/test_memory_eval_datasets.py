"""案例定义与运行转换：证据不能来自未来，标准答案不能进入对话输入。"""
import json
from pathlib import Path

import pytest

from tests.storage.sqlite.test_memory_eval_store import _dataset


def test_question_cannot_cite_a_later_session():
    from agent.types.eval.datasets import MemoryEvalDataset
    data = _dataset().model_dump(mode="json")
    case = data["cases"][0]
    case["history"].append({"session_id": "s2", "occurred_at": "2026-10-03T09:00:00+08:00",
                            "turns": [{"turn_id": "future", "user_text": "以后用 Go。"}]})
    case["questions"][0].update(history_session_count=1, evidence_turn_ids=["future"])
    with pytest.raises(ValueError, match="未来"):
        MemoryEvalDataset.model_validate(data)


def test_reference_answers_and_rubric_are_not_in_model_inputs():
    from agent.eval.datasets.memory_dataset import to_behavior_suite
    source = _dataset()
    question = source.cases[0].questions[0].model_copy(update={
        "reference_answer": "秘密标准答案 sentinel-answer-928", "required_meaning": ("sentinel-rubric-928",),
    })
    source = source.model_copy(update={"cases": (source.cases[0].model_copy(update={"questions": (question,)}),)})
    suite = to_behavior_suite(source)
    case = suite["cases"][0]
    inputs = json.dumps([case["sessions"], case["probe"]["text"]], ensure_ascii=False)
    assert "sentinel-answer-928" not in inputs
    assert "sentinel-rubric-928" not in inputs
    assert case["golden_question"]["requires_semantic_review"]
    assert suite["dataset_version"] == "1.0.0"


def test_baseline_import_preserves_all_existing_cases():
    from agent.eval.datasets.memory_dataset import load_defaults, to_behavior_suite
    first, _ = load_defaults()
    legacy = json.loads((Path(__file__).parent / "datasets/memory_behavior.json").read_text(encoding="utf-8"))
    suite = to_behavior_suite(first)
    assert first.version == "1.0.0"
    assert len(first.cases) == len(legacy["cases"]) == 23
    assert {case["id"] for case in suite["cases"]} == {case["id"] for case in legacy["cases"]}


def test_every_daily_case_has_multiple_sessions_natural_questions_and_evidence():
    from agent.eval.datasets.memory_dataset import load_defaults
    _, golden = load_defaults()
    assert golden.version == "2.0.0"
    assert len(golden.cases) == 20
    assert sum(len(case.questions) for case in golden.cases) >= 40
    for case in golden.cases:
        assert len(case.history) >= 2
        assert case.ignored_information
        for question in case.questions:
            assert question.evidence_turn_ids
            assert question.required_meaning and question.reference_answer
            assert question.requires_semantic_review
            assert not any(name in question.text for name in ("memory_search", "memory_read", "memory_edit"))


def test_optional_card_layout_still_rejects_forbidden_facts():
    from agent.eval.memory_behavior import score_cards
    card = {"item_id": "bad", "kind": "profile", "content": "用户已经使用Obsidian。"}
    score = score_cards([], [card], strict_count=False, forbidden_memories=[{
        "description": "建议不能成为已采用事实", "anchors": [["已经使用Obsidian"]],
    }])
    assert not score["passed"]
    assert score["issues"][0]["code"] == "forbidden_memory"


def test_keyword_success_does_not_mark_a_golden_question_semantically_passed(tmp_path, monkeypatch):
    from agent.eval.memory_behavior import run_suite
    from agent.model.registry import ModelRegistry
    from agent.types.model import ModelResponse

    class Model:
        name = "boundary_fixture"

        def chat(self, messages, tools=None, **kwargs):
            return ModelResponse(text='{"cards":[],"skipped":[]}' if "长期记忆提炼器" in messages[0]["content"] else "你好。")

    registry = ModelRegistry()
    registry.register(Model())
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    suite = dict(name="golden_review_gate", version="2.0.0", cases=[dict(
        id="greeting", description="语义标准不能靠关键词判通过", turns=["你好"], expected=[],
        probe=dict(text="周末怎么安排？", anchors=[["你好"]], tools=[]),
        golden_question=dict(requires_semantic_review=True, required_meaning=["根据明确约束制定计划"],
                             reference_answer="信息不足应先补问"),
    )])
    report = run_suite(suite, tmp_path / "run", wait_seconds=5)
    assert report["cases"][0]["automatic_checks_passed"]
    assert report["cases"][0]["review_status"] == "pending"
    assert report["summary"]["needs_semantic_review"] == 1
    assert not report["passed"]


def test_publishing_both_versions_does_not_expose_answers_as_user_memories(tmp_path):
    from agent.eval.datasets.memory_dataset import publish_defaults
    from agent.storage.sqlite.runtime_storage import RuntimeStorage
    from agent.types.memory.scope import MemoryScope
    from agent.storage.sqlite.memory_retrieval import MemoryRetriever
    path = tmp_path / "agent.db"
    first = publish_defaults(path)
    second = publish_defaults(path)
    assert [row["case_count"] for row in first["datasets"]] == [23, 20]
    assert all(not row["inserted"] for row in second["published"])
    assert Path(second["backup"]).is_file()
    runtime = RuntimeStorage.open(path)
    scope = MemoryScope(tenant_id="frontier", user_id="eval-user")
    assert runtime.memory.search(scope, "", limit=100) == ()
    assert MemoryRetriever(runtime.db).search(scope, "Harbor NorthStar 青禾区", source="all") == ()
