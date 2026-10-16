from __future__ import annotations

from dataclasses import replace
import time

import pytest

from agent.core.memory.config import MemoryExperienceConfig, MemoryReviewConfig
from agent.core.memory.memory_experience_judge import ExperienceJudgeError
from agent.core.memory.memory_feedback import MemoryFeedback, _excerpts
from agent.core.storage.sqlite.memory_card_decisions import SqliteMemoryCardDecisions
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message, ToolCall
from agent.types.memory.feedback import MemoryUsageResultItem, feedback_handler_version
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord


class _Judge:
    id = "fake-experience-judge-v1"

    def __init__(self, *, fail=False, refs=None, effect="helpful"):
        self.calls = []
        self.fail, self.refs, self.effect = fail, refs, effect

    def judge_usage(self, requests):
        self.calls.append(requests)
        if self.fail:
            raise ExperienceJudgeError("timeout")
        return [MemoryUsageResultItem(r.decision_id, "yes", self.effect, "observed_action_and_test_result",
                    self.refs if self.refs is not None else tuple(dict.fromkeys(e["reference"] for e in r.evidence)))
                for r in requests]


def _fixture(tmp_path, *, outcome="12 passed", before=False, with_answer=True,
             boundary=True, other_model=False, result_kind="completed"):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    storage.card_decisions = SqliteMemoryCardDecisions(storage.db)
    scope = MemoryScope(user_id="user")
    content = "一次观察：项目解释器缺少依赖时改用虚拟环境执行pytest，测试通过。"
    card = UserMemoryItem(item_id="card-one", kind="reusable_experience", scope=scope,
        source_type="test", source_ref="original", content=content, summary=content,
        content_hash=content_sha256(content), source_authority="model_inferred",
        keywords=("pytest", "依赖", "虚拟环境"), applicability="项目解释器缺少依赖时",
        action="使用项目虚拟环境执行pytest", outcome="一次测试通过", limitations="仅此项目一次观察",
        outcome_status="success")
    storage.memory.put(card)
    session = storage.sessions.get_or_create("feedback-test", scope=scope)
    user = session.add_message(Message(role="user", message_id="user-one", content_text="修复依赖并运行pytest", created_at=1))
    assistant = session.add_message(Message(role="assistant", message_id="request-one", created_at=2,
        content_text="改用项目虚拟环境执行测试", metadata={"iteration": 1,
            "tool_calls": [{"id": "provider-one", "name": "bash", "arguments_raw": '{"cmd":".venv/python -m pytest"}'}]}))
    tool = ToolCall(tool_call_id="fact-one", provider_tool_call_id="provider-one", session_id=session.session_id,
        trajectory_id="turn-one", tool_name="bash", arguments={"cmd": ".venv/python -m pytest"},
        status="success", result_text=outcome, created_at=2.1, finished_at=2.2)
    session.tool_calls.append(tool)
    session.add_message(Message(role="tool", message_id="result-one", content_text=outcome,
        created_at=2.2, tool_call_ids=[tool.tool_call_id], metadata={"tool_call_id": "provider-one"}))
    assistant_ids = [assistant.message_id]
    if with_answer:
        final = session.add_message(Message(role="assistant", message_id="answer-one", content_text="已使用虚拟环境运行pytest。", created_at=3))
        assistant_ids.append(final.message_id)
    turn = TurnRecord(turn_id="turn-one", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=tuple(assistant_ids), tool_call_ids=(tool.tool_call_id,),
        started_at=1, finished_at=4, completed=result_kind == "completed", interrupted=result_kind == "interrupted",
        error=None if result_kind == "completed" else "stopped",
        perception_trace={"builds": [{"iteration": 1, "records": [{"role": "user", "seq": user.seq_id}]}]})
    storage.sessions.commit_turn(session, turn, scope=scope)
    context = {"source_user_message_id": user.message_id}
    if boundary:
        context.update(first_iteration=1, exposed_at=2.5 if before else 1.5, after_seq=3 if before else 0)
    with storage.db.write() as conn:
        identity = storage.card_decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
            card_revision=1, turn_id=turn.turn_id, model_key="other-model" if other_model else "model-one", context=context)
    model_key = "other-model" if other_model else "model-one"
    storage.jobs.enqueue(job_type="memory.feedback", handler_version=feedback_handler_version(model_key),
        input={"turn_id": turn.turn_id, "model_key": model_key}, turn_id=turn.turn_id, session_id=session.session_id,
        dedupe_key=f"memory.feedback:{model_key}:{turn.turn_id}")
    return storage, identity, turn, scope


def test_feedback_binds_post_exposure_actions_and_real_fact_ids_once(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    judge = _Judge()
    worker = MemoryFeedback(storage, judge, "model-one")
    assert worker.run_once() is True
    assert worker.run_once() is False
    row = storage.card_decisions.get(identity)
    assert row["adopted"] == "yes" and row["effect"] == "helpful"
    assert "tool:fact-one" in row["evidence_refs"]
    assert all("provider-one" not in ref for ref in row["evidence_refs"])
    card = storage.memory.get("card-one")
    assert (card.used_count, card.judge_count, card.effective_count, card.helpful_count) == (1, 1, 1, 1)
    assert card.revision == 1
    assert judge.calls[0][0].card_revision == 1


def test_execution_success_alone_is_not_a_helpful_business_outcome(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path, outcome="success")
    worker = MemoryFeedback(storage, _Judge(), "model-one")
    assert worker.run_once()
    row = storage.card_decisions.get(identity)
    assert row["adopted"] == "yes" and row["effect"] == "unknown"
    assert storage.memory.get("card-one").helpful_count == 0


@pytest.mark.parametrize("result_kind", ["failed", "interrupted"])
def test_failed_and_interrupted_turns_preserve_observed_feedback(tmp_path, result_kind):
    storage, identity, _, _ = _fixture(tmp_path, outcome="pytest: 3 failed", result_kind=result_kind)
    assert MemoryFeedback(storage, _Judge(effect="harmful"), "model-one").run_once()
    row = storage.card_decisions.get(identity)
    assert row["adopted"] == "yes" and row["effect"] == "harmful"
    assert storage.memory.get("card-one").harmful_count == 1


def test_actions_before_exposure_do_not_prove_adoption(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path, before=True, with_answer=False)
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    assert not judge.calls
    row = storage.card_decisions.get(identity)
    assert row["adopted"] == "unknown" and row["effect"] == "unknown"


def test_missing_exposure_boundary_completes_unknown_without_judge(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path, boundary=False)
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    assert not judge.calls
    assert storage.card_decisions.get(identity)["reason"] == "missing_exposure_boundary"


def test_no_other_model_job_is_claimed(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path, other_model=True)
    judge = _Judge()
    assert not MemoryFeedback(storage, judge, "model-one").run_once()
    assert not judge.calls and storage.card_decisions.get(identity)["judged_at"] is None


def test_foreign_or_invented_evidence_cannot_update_statistics(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    assert MemoryFeedback(storage, _Judge(refs=("tool:invented",)), "model-one").run_once()
    assert storage.card_decisions.get(identity)["judged_at"] is None
    assert storage.memory.get("card-one").judge_count == 0
    assert storage.jobs.list(job_type="memory.feedback")[0].status == "retryable_failed"


def test_transport_failure_has_persistent_backoff_and_retry_limit(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    now = [time.time() + 1]
    judge = _Judge(fail=True)
    worker = MemoryFeedback(storage, judge, "model-one", clock=lambda: now[0],
                            review_config=MemoryReviewConfig(max_attempts=3, backoff_seconds=60))
    assert worker.run_once()
    assert not worker.run_once()
    now[0] += 61
    assert worker.run_once()
    now[0] += 61
    assert worker.run_once()
    now[0] += 61
    assert not worker.run_once()
    job = storage.jobs.list(job_type="memory.feedback")[0]
    assert job.status == "terminal_failed" and job.attempt_count == 3
    assert len(judge.calls) == 3
    assert storage.card_decisions.get(identity)["judged_at"] is None
    assert storage.memory.get("card-one").helpful_count == 0


def test_wrong_scope_snapshot_is_unknown_without_sending_data(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_card_decisions SET result_json=json_set(result_json,'$.context.scope.user_id','foreign') WHERE id=?", (identity,))
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    assert not judge.calls
    assert storage.card_decisions.get(identity)["reason"] == "invalid_exposure_scope"


def test_judge_sees_only_card_content_actually_shown_to_main_model(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_card_decisions SET result_json=json_set(result_json,'$.context.card_content','实际展示的一句建议','$.context.card.action','隐藏的未注入建议') WHERE id=?", (identity,))
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    visible = judge.calls[0][0].card
    assert visible["content"] == "实际展示的一句建议"
    assert "隐藏的未注入建议" not in str(visible)


def test_future_session_messages_are_not_evidence_for_old_turn(tmp_path):
    storage, identity, turn, scope = _fixture(tmp_path)
    session = storage.sessions.get_session(turn.session_id)
    user = session.add_message(Message(role="user", message_id="later-user", content_text="另一个项目已经部署成功", created_at=5))
    answer = session.add_message(Message(role="assistant", message_id="later-answer", content_text="另一个项目成功", created_at=6))
    later = TurnRecord(turn_id="later-turn", session_id=session.session_id, scope=scope, user_message_id=user.message_id,
        assistant_message_ids=(answer.message_id,), tool_call_ids=(), started_at=5, finished_at=6,
        completed=True, interrupted=False, error=None)
    storage.sessions.commit_turn(session, later, scope=scope)
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    refs = {e["reference"] for e in judge.calls[0][0].evidence}
    assert "message:later-answer" not in refs


def test_large_tool_output_preserves_relevant_middle_and_tail_with_offsets(tmp_path):
    text = "header\n" + "noise " * 400 + "pytest dependency error\n" + "noise " * 400 + "12 passed"
    storage, identity, _, _ = _fixture(tmp_path, outcome=text)
    judge = _Judge()
    assert MemoryFeedback(storage, judge, "model-one").run_once()
    request = judge.calls[0][0]
    outcome = next(e for e in request.evidence if e["kind"] == "outcome")
    excerpts = outcome["text"]["excerpts"]
    assert any("pytest dependency error" in e["text"] for e in excerpts)
    assert any("12 passed" in e["text"] for e in excerpts)
    assert all(text[e["offset"]:e["offset"] + len(e["text"])] == e["text"] for e in excerpts)
    assert not request.evidence_complete
    assert storage.card_decisions.get(identity)["effect"] == "unknown"


def test_disabled_feedback_leaves_job_untouched(tmp_path):
    storage, identity, _, _ = _fixture(tmp_path)
    assert not MemoryFeedback(storage, _Judge(), "model-one", replace(MemoryExperienceConfig(), enabled=False)).run_once()
    assert storage.jobs.list(job_type="memory.feedback")[0].attempt_count == 0


def test_more_than_three_exposures_continue_in_durable_batches(tmp_path):
    storage, _, turn, scope = _fixture(tmp_path)
    original = storage.memory.get("card-one")
    for number in range(2, 8):
        card = original.model_copy(update={"item_id": f"card-{number}", "used_count": 0})
        storage.memory.put(card)
        with storage.db.write() as conn:
            storage.card_decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
                card_revision=1, turn_id=turn.turn_id, model_key="model-one",
                context={"first_iteration": 1, "after_seq": 0, "exposed_at": 1.5,
                         "source_user_message_id": turn.user_message_id})
    judge = _Judge()
    worker = MemoryFeedback(storage, judge, "model-one")
    assert worker.run_once()
    assert len(storage.card_decisions.pending_usage(turn_id=turn.turn_id, model_key="model-one", limit=100)) == 4
    assert len(judge.calls) == 1 and len(judge.calls[0]) == 3
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    assert [len(batch) for batch in judge.calls] == [3, 3, 1]
    assert not storage.card_decisions.pending_usage(turn_id=turn.turn_id, model_key="model-one", limit=100)
    jobs = storage.jobs.list(job_type="memory.feedback")
    assert len(jobs) == 3 and all(job.status == "completed" for job in jobs)
    assert all(storage.memory.get(identity).judge_count == 1
               for identity in ["card-one", *[f"card-{n}" for n in range(2, 8)]])


def test_excerpts_keep_exact_offsets_when_windows_overlap():
    text = "abcdef关键xyz最后"
    assert _excerpts(text, ("关键",)) == [{"offset": 0, "text": text}]
