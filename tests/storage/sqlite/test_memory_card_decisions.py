from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.core.storage.sqlite.errors import IntegrityError, RevisionConflictError
from agent.core.storage.sqlite.memory_card_decisions import SqliteMemoryCardDecisions
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import EvidenceRef, TurnRecord


def _setup(tmp_path, *, metadata=None):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    card = storage.memory.put(UserMemoryItem(
        item_id="card-1", kind="reusable_experience", source_type="memory_extraction", source_ref="original",
        scope=scope, summary="pytest 验证后再改配置", content="pytest 验证后再改配置",
        content_hash=content_sha256("pytest 验证后再改配置"), source_authority="tool_verified",
        applicability="Windows 配置错误", action="先运行 pytest 验证", outcome="本次测试成功",
        limitations="只在本次 Windows 环境验证", outcome_status="success",
        keywords=("Windows", "pytest", "配置"), metadata=metadata or {},
        evidence=(EvidenceRef(source_type="message", source_id="original"),
                  EvidenceRef(source_type="tool_call", source_id="original-tool")),
    ))
    return storage, scope, card, SqliteMemoryCardDecisions(storage.db)


def _turn(storage, scope, number, *, completed=True, interrupted=False):
    session = storage.sessions.get_or_create("slot:" + scope.storage_key, scope=scope)
    user = session.add_message(Message(message_id=f"u-{number}", role="user", content_text="请修复配置",
                                       created_at=number * 10))
    assistant = session.add_message(Message(message_id=f"a-{number}", role="assistant", content_text="已按建议先验证",
                                            created_at=number * 10 + 1))
    turn = TurnRecord(turn_id=f"t-{number}", session_id=session.session_id, scope=scope,
                      user_message_id=user.message_id, assistant_message_ids=(assistant.message_id,),
                      tool_call_ids=(), started_at=number * 10, finished_at=number * 10 + 2,
                      completed=completed, interrupted=interrupted, error=None)
    storage.sessions.commit_turn(session, turn, scope=scope)
    return turn


def _expose(storage, scope, card, decisions, number, *, model="main-v1"):
    turn = _turn(storage, scope, number)
    with storage.db.write() as conn:
        identity = decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
                                              card_revision=card.revision, turn_id=turn.turn_id,
                                              model_key=model, card_content="当时实际注入的原文",
                                              context={"first_iteration": 1})
    return identity


def _judge(decisions, identity, number, effect="helpful", **kwargs):
    return decisions.judge_usage(identity, {"adopted": "yes", "effect": effect, "reason": "本轮确有引用"},
                                 evidence_refs=(f"message:a-{number}",), **kwargs)


def test_exposure_and_judgment_are_idempotent_and_keep_original_context(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    identity = _expose(storage, scope, card, decisions, 1)
    with storage.db.write() as conn:
        again = decisions.record_exposure(conn, scope=scope, card_id=card.item_id, card_revision=1,
                                           turn_id="t-1", model_key="main-v1", card_content="不能覆盖初始正文")
    assert identity == again
    assert storage.memory.get(card.item_id).used_count == 1
    assert len(decisions.pending_usage(turn_id="t-1", model_key="main-v1")) == 1
    result = _judge(decisions, identity, 1)
    assert _judge(decisions, identity, 1)["judged_at"] == result["judged_at"]
    current = storage.memory.get(card.item_id)
    assert (current.used_count, current.judge_count, current.effective_count, current.helpful_count) == (1, 1, 1, 1)
    assert current.maturity == .25 and current.revision == 1
    context = decisions.get(identity)["result_json"]["context"]
    assert context["card_content"] == "当时实际注入的原文"
    assert context["card"]["revision"] == 1 and context["first_iteration"] == 1
    assert decisions.pending_usage() == ()


def test_pending_waits_for_committed_turn_and_failed_parse_does_not_increment(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    with storage.db.write() as conn:
        identity = decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
                                              card_revision=1, turn_id="t-1", model_key="main-v1")
    assert decisions.pending_usage() == ()
    _turn(storage, scope, 1)
    assert len(decisions.pending_usage()) == 1
    with pytest.raises(ValueError):
        decisions.judge_usage(identity, {"adopted": "yes", "effect": "helpful"})
    assert storage.memory.get(card.item_id).judge_count == 0


def test_rejudgment_applies_difference_with_compare_and_swap(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    identity = _expose(storage, scope, card, decisions, 1)
    first = _judge(decisions, identity, 1)
    with pytest.raises(RevisionConflictError):
        _judge(decisions, identity, 1, "harmful")
    second = _judge(decisions, identity, 1, "harmful", expected_judged_at=first["judged_at"])
    current = storage.memory.get(card.item_id)
    assert second["judged_at"] > first["judged_at"]
    assert (current.judge_count, current.effective_count, current.helpful_count, current.harmful_count) == (1, 1, 0, 1)
    with pytest.raises(RevisionConflictError):
        _judge(decisions, identity, 1, expected_judged_at=first["judged_at"])


def test_unknown_and_not_adopted_are_not_negative_results(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    first = _expose(storage, scope, card, decisions, 1)
    decisions.judge_usage(first, {"adopted": "unknown", "effect": "unknown"})
    second = _expose(storage, scope, card, decisions, 2)
    decisions.judge_usage(second, {"adopted": "no", "effect": "unknown"})
    third = _expose(storage, scope, card, decisions, 3)
    _judge(decisions, third, 3, "unknown")
    current = storage.memory.get(card.item_id)
    assert (current.used_count, current.judge_count, current.effective_count) == (3, 3, 1)
    assert current.helpful_count == current.harmful_count == 0 and current.recall_enabled


def test_evidence_only_revision_preserves_stats_but_semantic_revision_resets(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path, metadata={"embedding": {"values": [.1, .2]}})
    first = _expose(storage, scope, card, decisions, 1)
    _judge(decisions, first, 1)
    pending = _expose(storage, scope, card, decisions, 2)
    # 旧对象中的零计数不能覆盖后台已经提交的累计值。
    supplemented = storage.memory.put(card.model_copy(update={
        "revision": 2, "evidence": (*card.evidence, EvidenceRef(source_type="message", source_id="new-source"))
    }), expected_revision=1)
    assert supplemented.used_count == 2 and supplemented.helpful_count == 1
    _judge(decisions, pending, 2)
    assert storage.memory.get(card.item_id).helpful_count == 2
    old_pending = _expose(storage, scope, supplemented, decisions, 3)
    updated = storage.memory.put(supplemented.model_copy(update={
        "revision": 3, "applicability": "仅适用于 Linux 配置错误"
    }), expected_revision=2)
    assert updated.helpful_count == updated.used_count == 0 and "embedding" not in updated.metadata
    assert updated.metadata["stats_generation_start"] == 3
    assert _judge(decisions, old_pending, 3)["counted_at"] is None
    assert storage.memory.get(card.item_id).judge_count == 0


def test_rebuild_is_model_specific_and_keeps_evidence_revision_generation(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    one = _expose(storage, scope, card, decisions, 1)
    _judge(decisions, one, 1)
    two = _expose(storage, scope, card, decisions, 2, model="main-v2")
    _judge(decisions, two, 2, "harmful")
    assert storage.memory.get(card.item_id).helpful_count == 1
    storage.memory.put(card.model_copy(update={"revision": 2, "keywords": (*card.keywords, "验证")}), expected_revision=1)
    decisions.rebuild_stats("main-v2")
    current = storage.memory.get(card.item_id)
    assert (current.used_count, current.helpful_count, current.harmful_count) == (1, 0, 1)
    decisions.rebuild_stats("main-v1")
    current = storage.memory.get(card.item_id)
    assert (current.used_count, current.helpful_count, current.harmful_count) == (1, 1, 0)
    assert current.revision == 2


def test_harmful_feedback_disables_only_automatic_recall_and_rebuild_restores(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    for number in range(1, 6):
        identity = _expose(storage, scope, card, decisions, number)
        _judge(decisions, identity, number, "harmful")
    current = storage.memory.get(card.item_id)
    assert not current.recall_enabled and current.status.value == "active"
    assert current.metadata["recall_disable_reason"] == "harmful_feedback"
    decisions.rebuild_stats("new-main")
    assert storage.memory.get(card.item_id).recall_enabled
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_cards SET recall_enabled=0 WHERE id=?", (card.item_id,))
    decisions.rebuild_stats("another-main")
    assert not storage.memory.get(card.item_id).recall_enabled


def test_scene_trial_waits_for_judgments_and_disables_unproven_candidate(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path, metadata={"merged_from": [{"card_id": "parent"}]})
    identities = [_expose(storage, scope, card, decisions, number) for number in range(1, 11)]
    assert storage.memory.get(card.item_id).recall_enabled
    for identity in identities[:9]:
        decisions.judge_usage(identity, {"adopted": "unknown", "effect": "unknown"})
    assert storage.memory.get(card.item_id).recall_enabled
    decisions.judge_usage(identities[-1], {"adopted": "unknown", "effect": "unknown"})
    current = storage.memory.get(card.item_id)
    assert not current.recall_enabled and current.metadata["recall_disable_reason"] == "merge_trial_failed"


def test_scope_and_turn_evidence_cannot_be_fabricated(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    first = _expose(storage, scope, card, decisions, 1)
    _turn(storage, scope, 2)
    with pytest.raises(IntegrityError, match="outside turn"):
        _judge(decisions, first, 2)
    with storage.db.write() as conn:
        assert decisions.record_exposure(conn, scope=MemoryScope(user_id="other"), card_id=card.item_id,
                                          card_revision=1, turn_id="t-1", model_key="main-v1") is None
    assert storage.memory.get(card.item_id).judge_count == 0


def test_relation_and_merge_share_only_one_new_table_and_keep_context(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    with storage.db.write() as conn:
        first = decisions.record_decision(conn, scope=scope, card_id=card.item_id, card_revision=1,
                                           decision_type="merge", model_key="merge-model", dedupe_key="pair:1",
                                           context={"stage": "relation", "parents": [card.model_dump(mode="json")]})
        duplicate = decisions.record_decision(conn, scope=scope, card_id=card.item_id, card_revision=1,
                                               decision_type="merge", model_key="merge-model", dedupe_key="pair:1",
                                               context={"parents": []})
        assert first["inserted"] and not duplicate["inserted"] and first["id"] == duplicate["id"]
        decisions.complete_decision(conn, first["id"], {"relation": "complementary"})
    stored = decisions.get_by_key("pair:1")
    assert len(stored["result_json"]["context"]["parents"]) == 1
    assert stored["result_json"]["judgment"]["relation"] == "complementary"
    assert storage.memory.get(card.item_id).used_count == 0
    with storage.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name LIKE 'memory_card_decision%'").fetchone()[0] == 1


def test_source_ids_are_computed_and_roundtrip_with_dump(tmp_path):
    storage, _, card, _ = _setup(tmp_path)
    dumped = card.model_dump(mode="json")
    assert dumped["source_message_ids"] == ["original"] and dumped["maturity"] == 0
    assert UserMemoryItem.model_validate(dumped).source_message_ids == ("original",)
    assert storage.memory.history(card.item_id)[0].source_message_ids == ("original",)


def test_failed_and_interrupted_turns_are_not_dropped_from_feedback(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    for number in (1, 2):
        with storage.db.write() as conn:
            decisions.record_exposure(conn, scope=scope, card_id=card.item_id, card_revision=1,
                                       turn_id=f"t-{number}", model_key="main-v1")
        _turn(storage, scope, number, completed=False, interrupted=number == 2)
    rows = decisions.pending_usage()
    assert {row["turn_id"] for row in rows} == {"t-1", "t-2"}
    for row in rows:
        decisions.judge_usage(row["id"], {"adopted": "unknown", "effect": "unknown"})
    current = storage.memory.get(card.item_id)
    assert current.judge_count == 2 and current.harmful_count == 0


def test_rebuild_does_not_restore_expired_automatically_disabled_card(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    for number in range(1, 6):
        _judge(decisions, _expose(storage, scope, card, decisions, number), number, "harmful")
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_cards SET valid_until=1 WHERE id=?", (card.item_id,))
    decisions.rebuild_stats("new-main")
    assert not storage.memory.get(card.item_id).recall_enabled


def test_same_turn_evidence_revision_reuses_first_exposure_and_rebuild(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    first = _expose(storage, scope, card, decisions, 1)
    updated = storage.memory.put(card.model_copy(update={
        "revision": 2, "evidence": (*card.evidence, EvidenceRef(source_type="message", source_id="another"))
    }), expected_revision=1)
    with storage.db.write() as conn:
        second = decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
                                            card_revision=updated.revision, turn_id="t-1", model_key="main-v1",
                                            card_content="另一次来源补充，不应替换首次可见正文")
    assert first == second
    assert storage.memory.get(card.item_id).used_count == 1
    assert decisions.get(first)["result_json"]["context"]["card_content"] == "当时实际注入的原文"
    decisions.rebuild_stats("main-v1")
    assert storage.memory.get(card.item_id).used_count == 1


def test_same_turn_new_semantic_generation_has_its_own_exposure(tmp_path):
    storage, scope, card, decisions = _setup(tmp_path)
    first = _expose(storage, scope, card, decisions, 1)
    updated = storage.memory.put(card.model_copy(update={"revision": 2, "action": "改成另一项实际做法"}),
                                 expected_revision=1)
    with storage.db.write() as conn:
        second = decisions.record_exposure(conn, scope=scope, card_id=card.item_id,
                                            card_revision=updated.revision, turn_id="t-1", model_key="main-v1")
    assert first != second
    assert decisions.get(first)["card_revision"] == 1 and decisions.get(second)["card_revision"] == 2
    assert storage.memory.get(card.item_id).used_count == 1
    _judge(decisions, first, 1)
    assert storage.memory.get(card.item_id).judge_count == 0
    decisions.rebuild_stats("main-v1")
    assert storage.memory.get(card.item_id).used_count == 1
