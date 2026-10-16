"""Slice 9/10: SQLite SessionStore——恢复、无损持久化与原子 reset（§6）。

公共 seam：SessionStore（SqliteSessionStore）。真实文件 SQLite，不 mock sqlite3；
断言用原 Pydantic model dump，不直接 SELECT 表（§2 Seam E）。
"""
import pytest

from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.context import Anchor, AnchorFact
from agent.types.conversation import ContentPart, Message, Session, ToolCall
from agent.types.memory.scope import MemoryScope
from agent.core.storage.sqlite.errors import AuditConflictError, SessionIntegrityError
from agent.core.storage.sqlite.session_store import SqliteSessionStore
from agent.types.memory.source import TurnRecord

_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


def _turn(session: Session, *, turn_id: str = "t1") -> TurnRecord:
    return TurnRecord(
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


def test_saved_session_is_restored_by_new_store_instance(tmp_path):
    """commit_turn 落库后，新 Store 实例（=进程重启）能按 session_key 恢复 Session。"""
    db = tmp_path / "memory.db"
    store1 = RuntimeStorage.open(db).sessions
    session = store1.get_or_create(
        "cli:dm:u1", scope=_SCOPE, channel="cli", author_id="u1"
    )
    session.add_message(Message(role="user", content_text="你好，记住我"))
    store1.commit_turn(session, _turn(session), scope=_SCOPE)

    store2 = RuntimeStorage.open(db).sessions  # 新实例，模拟重启
    restored = store2.find("cli:dm:u1")

    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.session_key == "cli:dm:u1"
    assert restored.channel == "cli"
    assert restored.author_id == "u1"
    assert [m.content_text for m in restored.messages] == ["你好，记住我"]
    assert restored.messages[0].message_id == session.messages[0].message_id
    assert restored.message_count == 1


def test_structured_context_anchor_roundtrips_in_session_payload(tmp_path):
    """context_anchor 随 Session payload 持久化，不需要新增数据库表。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("anchor", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="问题"))
    session.context_anchor = Anchor(
        anchor_id="anchor-1",
        session_id=session.session_id,
        covered_start_seq=0,
        covered_end_seq=0,
        covered_start_message_id=session.messages[0].message_id,
        covered_end_message_id=session.messages[0].message_id,
        covered_turn_count=1,
        source_digest="digest",
        intent=(AnchorFact(
            text="问题",
            source_message_ids=(session.messages[0].message_id,),
        ),),
    )
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    restored = RuntimeStorage.open(db).sessions.find("anchor")
    assert restored is not None
    assert restored.context_anchor == session.context_anchor
    assert isinstance(restored.context_anchor, Anchor)


def test_invalid_context_anchor_payload_does_not_block_session_restore(tmp_path):
    """损坏旧 Anchor 保留为字典，由 Perception 恢复原始历史。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("invalid-anchor", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="问题"))
    session.context_anchor = {"version": 2, "anchor_id": "broken"}
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    restored = RuntimeStorage.open(db).sessions.find("invalid-anchor")
    assert restored is not None
    assert restored.context_anchor == {"version": 2, "anchor_id": "broken"}


def test_repeated_commit_turn_is_idempotent(tmp_path):
    """§6.3：相同 turn_id + 相同 payload 重复 commit 幂等成功，消息不翻倍。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="问题"))
    turn = _turn(session)

    store.commit_turn(session, turn, scope=_SCOPE)
    store.commit_turn(session, turn, scope=_SCOPE)  # 重复提交同一 turn

    restored = RuntimeStorage.open(db).sessions.find("k")
    assert restored is not None
    assert [m.content_text for m in restored.messages] == ["问题"]
    assert restored.message_count == 1


def test_find_returns_none_for_unknown_session_key(tmp_path):
    """未知 session_key 不自动创建，返回 None。"""
    store = RuntimeStorage.open(tmp_path / "memory.db").sessions
    store.get_or_create("known", scope=_SCOPE)

    assert store.find("unknown") is None
    assert store.find("unknown") is None  # 重复 find 也不产生副作用


def test_wal_and_foreign_keys_are_enabled(tmp_path):
    """§6.1：连接工厂产生的每个连接都满足 WAL / foreign_keys / busy_timeout 契约。"""
    store = RuntimeStorage.open(tmp_path / "memory.db").sessions

    info = store.diagnostics()

    assert info["journal_mode"] == "wal"
    assert info["foreign_keys"] is True
    assert info["busy_timeout"] == 5000


# ---- Slice 10：无损持久化与 reset ----


def test_message_content_parts_and_metadata_roundtrip_losslessly(tmp_path):
    """§6.2/ADR-6：ContentPart 多模态字段与嵌套 metadata 经 model_dump_json 无损往返。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE, channel="cli", author_id="u1")
    original = Message(
        role="user",
        author_id="u1",
        trajectory_id="traj-1",
        content=[
            ContentPart(type="text", text="看这张图"),
            ContentPart(
                type="image", url="https://example.com/a.png",
                data={"width": 800, "tags": ["截图", "报错"]},
            ),
        ],
        content_text="看这张图",
        tool_call_ids=["tc-9"],
        metadata={"platform": {"name": "飞书", "chat": {"id": "oc_1", "type": "group"}},
                  "flags": [True, False], "score": 0.75},
        created_at=1784808000.5,
    )
    session.add_message(original)
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    restored = RuntimeStorage.open(db).sessions.find("k")
    assert len(restored.messages) == 1
    assert restored.messages[0].model_dump(mode="json") == original.model_dump(mode="json")


def test_tool_call_result_and_error_roundtrip_losslessly(tmp_path):
    """§6.2：ToolCall 的结构化结果、错误与 metadata 经 payload_json 无损往返。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="算一下"))
    ok_call = ToolCall(
        tool_call_id="tc-1", trajectory_id="traj-1", tool_name="calculator",
        arguments={"expression": "1+1"}, status="success",
        created_at=1784808000.0, finished_at=1784808001.0, durations_ms=1000,
        tool_call_result={"value": 2, "steps": ["1+1"]}, result_text="2",
        metadata={"engine": "python"},
    )
    err_call = ToolCall(
        tool_call_id="tc-2", trajectory_id="traj-1", tool_name="search",
        arguments={"q": "x"}, status="error",
        created_at=1784808002.0, finished_at=1784808003.5, durations_ms=1500,
        error="HTTP 503: 服务不可用", metadata={"retries": 2},
    )
    session.tool_calls.extend([ok_call, err_call])
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    restored = RuntimeStorage.open(db).sessions.find("k")
    assert [tc.model_dump(mode="json") for tc in restored.tool_calls] == [
        ok_call.model_dump(mode="json"),
        err_call.model_dump(mode="json"),
    ]


def test_completed_message_recommit_with_identical_payload_is_idempotent(tmp_path):
    """§6.3/ADR-12：后续 commit 携带未变的已完成消息 = 幂等成功，不重复、不报错。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="第一问"))
    store.commit_turn(session, _turn(session, turn_id="t1"), scope=_SCOPE)

    session.add_message(Message(role="assistant", content_text="第一答"))
    turn2 = _turn(session, turn_id="t2")
    store.commit_turn(session, turn2, scope=_SCOPE)  # t1 消息原样再次提交

    restored = RuntimeStorage.open(db).sessions.find("k")
    assert [m.content_text for m in restored.messages] == ["第一问", "第一答"]


def test_completed_message_recommit_with_changed_payload_raises_audit_conflict(tmp_path):
    """§6.3/ADR-12：相同 message_id 不同 payload 重提必须暴露冲突，禁止静默覆盖。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    original = Message(role="user", content_text="原始内容")
    session.add_message(original)
    store.commit_turn(session, _turn(session, turn_id="t1"), scope=_SCOPE)

    tampered = original.model_copy(update={"content_text": "篡改内容"})
    session.messages[0] = tampered
    with pytest.raises(AuditConflictError):
        store.commit_turn(session, _turn(session, turn_id="t2"), scope=_SCOPE)

    restored = RuntimeStorage.open(db).sessions.find("k")
    assert [m.content_text for m in restored.messages] == ["原始内容"]  # 未被覆盖


def test_close_and_reset_atomically_switches_active_session_and_preserves_old_session(tmp_path):
    """§6.3：一个事务内关闭旧 session + 创建新 session + 切换 slot；旧数据保留。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE, channel="cli", author_id="u1")
    session.add_message(Message(role="user", content_text="旧会话消息"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    result = store.close_and_reset(
        "k", session, scope=_SCOPE, channel="cli", author_id="u1"
    )

    assert result.closed_session is not None
    assert result.closed_session.session_id == session.session_id
    assert result.closed_session.status == "completed"
    assert [m.content_text for m in result.closed_session.messages] == ["旧会话消息"]
    assert result.new_session.session_id != session.session_id
    assert result.new_session.messages == []
    assert result.new_session.session_key == "k"

    active = RuntimeStorage.open(db).sessions.find("k")  # 新实例验证切换已落库
    assert active is not None
    assert active.session_id == result.new_session.session_id
    assert active.messages == []


def test_close_and_reset_persists_unsaved_old_messages_before_switch(tmp_path):
    """§6.3-2/ADR-13：切换前必须把调用方最新状态（含未 commit 消息）落库。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="已提交"))
    store.commit_turn(session, _turn(session, turn_id="t1"), scope=_SCOPE)
    session.add_message(Message(role="user", content_text="尚未提交"))  # 从未 commit

    result = store.close_and_reset("k", session, scope=_SCOPE)

    assert [m.content_text for m in result.closed_session.messages] == [
        "已提交", "尚未提交",
    ]
    # 持久化探针（只经公开 seam，不 SELECT）：新实例对旧会话那条「尚未提交」
    # 消息做异 payload 重提——若切换时已落库，必须报 audit_conflict
    tampered = result.closed_session.model_copy(deep=True)
    tampered.messages[1].content_text = "篡改"
    with pytest.raises(AuditConflictError):
        RuntimeStorage.open(db).sessions.commit_turn(
            tampered, _turn(tampered, turn_id="t2"), scope=_SCOPE
        )


def _explode_id() -> str:
    raise RuntimeError("boom")


def test_failed_close_and_reset_does_not_fire_session_end_hook(tmp_path):
    """§3.3：事务失败整体回滚——无 SessionReset（Gateway 无从发 hook），旧 active 不变。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="旧消息"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    exploding = RuntimeStorage.open(db, id_factory=_explode_id).sessions
    with pytest.raises(RuntimeError, match="boom"):
        exploding.close_and_reset("k", session, scope=_SCOPE)

    active = RuntimeStorage.open(db).sessions.find("k")
    assert active is not None
    assert active.session_id == session.session_id  # slot 未切换
    assert active.status == "active"                # 旧 session 未被标记 completed
    assert [m.content_text for m in active.messages] == ["旧消息"]


def test_slot_scope_mismatch_is_rejected_as_integrity_error(tmp_path):
    """§6.3：get_or_create 传入 scope 与 slot 记录不一致 → 完整性错误，不跨 scope 恢复。"""
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    store.get_or_create("k", scope=_SCOPE)

    other = MemoryScope(tenant_id="cli:chat:c1")
    with pytest.raises(SessionIntegrityError):
        store.get_or_create("k", scope=other)

    # 原 scope 不受影响，仍正常恢复
    assert store.find("k") is not None
