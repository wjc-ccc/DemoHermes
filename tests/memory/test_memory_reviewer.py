"""Slice 13: 自动 Background Reviewer（§8.2）。

公共 seam：MemoryReviewer + SessionStore outbox + Manager。
真实 Reviewer/调度器，在其模型抽取 seam 注入 FakeExtractor；不 Fake Reviewer 本身，不 mock Manager。
reviewer 结果通过 manager.read/search 验证；outbox 原子性这类直接观察 outbox。
"""
import pytest

from Agent.core.types import Message
from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.memory.episodic.store import AuditConflictError, SqliteSessionStore
from Agent.memory.reviewer import MemoryReviewer
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="t_001")


class _FakeExtractor:
    """返回固定 mutation 列表，忽略 turn 内容（§8.2 外部 seam）。"""

    def __init__(self, mutations) -> None:
        self._mutations = list(mutations)

    def extract(self, turn: TurnEnvelope):
        return list(self._mutations)


def _turn(session, *, turn_id="t1") -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=_SCOPE,
        user_message_id=session.messages[-1].message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_commit_turn_atomically_creates_pending_review_job(tmp_path):
    """§8.2：commit_turn 与 pending review job 同事务落库，可观察到一条 pending 任务。"""
    store = SqliteSessionStore(tmp_path / "memory.db")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="讨论部署"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    pending = store.pending_review_jobs()
    assert len(pending) == 1
    job = pending[0]
    assert job.turn_id == "t1"
    assert job.status == "pending"
    assert job.session_id == session.session_id


def test_commit_failure_creates_neither_session_changes_nor_review_job(tmp_path):
    """§8.2：commit_turn 失败时事务整体回滚--不改 session、不创建 review job。"""
    store = SqliteSessionStore(tmp_path / "memory.db")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="原始", message_id="m1"))
    store.commit_turn(session, _turn(session, turn_id="t1"), scope=_SCOPE)

    # 制造 audit 冲突：同 message_id 不同 payload -> commit_turn 抛错并整体回滚
    session.messages[0].content_text = "篡改"
    with pytest.raises(AuditConflictError):
        store.commit_turn(session, _turn(session, turn_id="t2"), scope=_SCOPE)

    # 回滚后：未创建 turn2 的 review job，仍只有 turn1
    assert [j.turn_id for j in store.pending_review_jobs()] == ["t1"]
    # session 未被篡改：从库恢复仍是 “原始”
    restored = store.find("k")
    assert restored.messages[0].content_text == "原始"


def test_reviewer_daily_decision_creates_daily_entry(tmp_path):
    """§8.2：reviewer 消费 pending job，把 daily 决策经 Manager.apply 落库。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="今天部署到 staging"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    extractor = _FakeExtractor([
        BuiltinMemoryMutation(
            scope=_SCOPE, target="daily", action="add", date="2026-07-23",
            content="部署到 staging", provenance=_PROVENANCE,
        )
    ])
    reviewer = MemoryReviewer(store, manager, extractor)
    processed = reviewer.process_pending()

    assert processed == 1
    view = manager.read(
        BuiltinMemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert any("部署到 staging" in e.content for e in view.entries)
    assert store.pending_review_jobs() == []  # job 已 done


def test_reviewer_stable_preference_updates_profile(tmp_path):
    """§8.2：reviewer 把稳定用户事实提炼为 profile，经 Manager.apply 落库。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="我喜欢简洁"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    MemoryReviewer(store, manager, _FakeExtractor([BuiltinMemoryMutation(
        scope=_SCOPE, target="profile", action="add",
        content="偏好简洁回答", provenance=_PROVENANCE,
    )])).process_pending()

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert any("偏好简洁回答" in e.content for e in view.entries)


def test_reviewer_one_off_fact_creates_no_hot_memory(tmp_path):
    """§8.2：一次性问题不写 Hot；extractor 返回空 -> 无记忆写入，job 仍 done。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="今天天气不错"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    MemoryReviewer(store, manager, _FakeExtractor([])).process_pending()

    profile = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    memory = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert len(profile.entries) == 0
    assert len(memory.entries) == 0
    assert store.pending_review_jobs() == []  # 无 mutation 也是成功完成


def test_reviewer_invalid_mutation_is_rejected_by_manager(tmp_path):
    """§8.2：reviewer 产出的非法 mutation 被 Manager 拒绝（ok=False），不写入。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="随便"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    MemoryReviewer(store, manager, _FakeExtractor([BuiltinMemoryMutation(
        scope=_SCOPE, target="profile", action="add",
        content="含保留标记 <!-- memory-entry:evil -->", provenance=_PROVENANCE,
    )])).process_pending()

    profile = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert len(profile.entries) == 0  # 被 Manager 拒绝，未写入


def test_duplicate_review_delivery_produces_one_memory_result(tmp_path):
    """§8.2：重复投递同一 turn 的 review 只产生一个可观察记忆结果（daily add 内容去重）。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="部署到 staging"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    mutation = BuiltinMemoryMutation(
        scope=_SCOPE, target="daily", action="add", date="2026-07-23",
        content="部署到 staging", provenance=_PROVENANCE,
    )
    reviewer = MemoryReviewer(store, manager, _FakeExtractor([mutation]))
    reviewer.process_pending()  # 第一次投递

    store.requeue_review_job("t1")  # 模拟重复投递（at-least-once）
    reviewer.process_pending()  # 第二次投递，同内容

    view = manager.read(
        BuiltinMemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert len(view.entries) == 1  # 只一条，重复投递未产生第二条


def test_reviewer_failure_is_retried_then_marked_failed(tmp_path):
    """§8.2：reviewer 失败按有界退避重试，超 max_attempts 标 failed（不再 pending）。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="会失败"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    class _BoomExtractor:
        def extract(self, turn):
            raise RuntimeError("extract failed")

    reviewer = MemoryReviewer(
        store, manager, _BoomExtractor(),
        clock=lambda: 2000.0, max_attempts=3, backoff_seconds=0.0,
    )
    reviewer.process_pending()

    assert store.review_job_status("t1") == "failed"


def test_expired_running_job_lease_is_reclaimed_after_restart(tmp_path):
    """§8.2：lease 已过期的 running job 在重启后可被回收再处理。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="崩溃前的话"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    # 模拟崩溃：认领后进程死，job 留在 running，lease_until=2010
    store.claim_review_job(worker_id="w1", now=2000.0, lease_seconds=10.0)
    assert store.review_job_status("t1") == "running"

    # 重启后 now=3000 > lease 2010：新 reviewer 回收 expired running job 并处理
    MemoryReviewer(
        store, manager,
        _FakeExtractor([BuiltinMemoryMutation(
            scope=_SCOPE, target="daily", action="add", date="2026-07-23",
            content="崩溃后回顾", provenance=_PROVENANCE,
        )]),
        clock=lambda: 3000.0,
    ).process_pending()

    assert store.review_job_status("t1") == "done"


def test_reviewer_failure_does_not_break_reply(tmp_path):
    """§8.3：reviewer 单 job 失败被 containment--process_pending 不抛错，健康 job 仍完成。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    s1 = store.get_or_create("k1", scope=_SCOPE)
    s1.add_message(Message(role="user", content_text="坏"))
    store.commit_turn(s1, _turn(s1, turn_id="t1"), scope=_SCOPE)
    s2 = store.get_or_create("k2", scope=_SCOPE)
    s2.add_message(Message(role="user", content_text="好"))
    store.commit_turn(s2, _turn(s2, turn_id="t2"), scope=_SCOPE)

    class _SelectiveExtractor:
        def extract(self, turn):
            if turn.turn_id == "t1":
                raise RuntimeError("fail t1")
            return [BuiltinMemoryMutation(
                scope=_SCOPE, target="profile", action="add",
                content="好 turn 偏好", provenance=_PROVENANCE,
            )]

    reviewer = MemoryReviewer(
        store, manager, _SelectiveExtractor(),
        clock=lambda: 2000.0, max_attempts=1, backoff_seconds=0.0,
    )
    processed = reviewer.process_pending()  # 不抛错

    assert processed == 2
    assert store.review_job_status("t1") == "failed"
    assert store.review_job_status("t2") == "done"


def test_session_end_runs_final_review_after_reset_commit(tmp_path):
    """§8.3：close_and_reset 提交后，旧 session 的 pending review 仍可被 final review 处理。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="最后的话"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    # reset：原子关闭旧 session、建新 session（旧 session 的 review job 保留）
    reset = store.close_and_reset("k", session, scope=_SCOPE)
    assert reset.closed_session is not None

    # final review：处理旧 session 遗留的 pending job
    MemoryReviewer(store, manager, _FakeExtractor([BuiltinMemoryMutation(
        scope=_SCOPE, target="daily", action="add", date="2026-07-23",
        content="session 结束回顾", provenance=_PROVENANCE,
    )]), clock=lambda: 2000.0).process_pending()

    assert store.review_job_status("t1") == "done"
    view = manager.read(
        BuiltinMemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert any("session 结束回顾" in e.content for e in view.entries)
