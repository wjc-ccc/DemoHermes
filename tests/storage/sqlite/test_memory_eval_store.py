"""案例版本是公开存储契约：旧版可重读，同版不能被另一份答案覆盖。"""
import pytest

from agent.storage.sqlite.runtime_storage import RuntimeStorage


def _dataset(version="1.0.0", text="我是 Python 后端工程师。"):
    from agent.types.eval.datasets import MemoryEvalDataset
    return MemoryEvalDataset.model_validate({
        "dataset_id": "memory_behavior", "version": version, "title": "记忆案例",
        "description": "测试案例版本", "created_at": "2026-10-08T10:00:00+08:00",
        "cases": [{
            "case_id": "profession", "title": "职业", "scenario": "work",
            "difficulty": "basic", "abilities": ["extraction"],
            "history": [{"session_id": "s1", "occurred_at": "2026-10-01T09:00:00+08:00",
                         "turns": [{"turn_id": "t1", "user_text": text}]}],
            "questions": [{"question_id": "q1", "text": "我的职业是什么？",
                           "asked_at": "2026-10-02T09:00:00+08:00",
                           "reference_answer": "Python 后端工程师", "required_meaning": ["说明当前职业"],
                           "evidence_turn_ids": ["t1"]}],
        }],
    })


def test_two_versions_keep_their_own_history_questions_and_answers_after_reopen(tmp_path):
    path = tmp_path / "agent.db"
    store = RuntimeStorage.open(path).memory_evals
    first, second = _dataset(), _dataset("2.0.0", "我是 Go 后端工程师。")
    assert store.publish(first)
    assert not store.publish(first)
    assert store.publish(second)
    reopened = RuntimeStorage.open(path).memory_evals
    assert reopened.get_dataset("memory_behavior", "1.0.0") == first
    assert reopened.get_dataset("memory_behavior", "2.0.0") == second
    assert [row["dataset_version"] for row in reopened.list_datasets("memory_behavior")] == ["1.0.0", "2.0.0"]


def test_published_version_cannot_be_silently_replaced(tmp_path):
    from agent.storage.sqlite.memory_eval_store import DatasetVersionConflictError
    store = RuntimeStorage.open(tmp_path / "agent.db").memory_evals
    first = _dataset()
    store.publish(first)
    with pytest.raises(DatasetVersionConflictError):
        store.publish(_dataset(text="我是 Go 后端工程师。"))
    assert store.get_dataset("memory_behavior", "1.0.0") == first
