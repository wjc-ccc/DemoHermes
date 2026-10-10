from dataclasses import replace
"""Gateway、冻结 MemorySnapshot 与 SQLite Session 的端到端接线。"""
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.storage.sqlite.database import connect
from agent.channel.channel_cli import CliChannel
from tests.loop_fakes import make_loop
from agent.core.event_bus import EventBus
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.core.memory.memory_scope import build_memory_scope, ensure_scope_dir
from agent.types.channel import InboundEvent, SessionSource
from agent.types.conversation import Message, ToolCall
from agent.gateway.gateway import Gateway
from agent.gateway.session_key import build_session_key
from agent.types.model import ModelResponse
from agent.storage.sqlite.session_store import SqliteSessionArchiveReader, SqliteSessionStore


class _MockModel:
    name = "mock"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _event(text: str) -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(
            channel="cli", chat_id="local", chat_type="dm", user_id="u1"
        ),
    )


def _make_gateway(script, *, store, memory_context, on_session_end=None):
    loop = make_loop(model=_MockModel(script), event_bus=EventBus())
    gateway = Gateway(
        loop=loop,
        store=store,
        memory_context=memory_context,
        workers=1,
        on_session_end=on_session_end,
    )
    gateway.register_channel(CliChannel())
    gateway.start()
    return gateway, loop


def test_gateway_freezes_plain_user_book_and_persists_session(tmp_path):
    db = tmp_path / "memory.db"
    root = tmp_path / "memory"
    scope = build_memory_scope(_event("你好").source)
    directory = ensure_scope_dir(root, scope)
    (directory / "MEMORY.md").write_text("用户偏好简洁", encoding="utf-8")
    gateway, loop = _make_gateway(
        [ModelResponse(text="收到")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(root),
    )
    try:
        reply = gateway.ask(_event("你好"), timeout=10)
    finally:
        gateway.stop()

    assert reply.text == "收到"
    assert any("用户偏好简洁" in str(message) for message in loop.model.calls[0])
    key = build_session_key(_event("你好").source, agent_id="main")
    restored = RuntimeStorage.open(db).sessions.find(key)
    assert restored is not None
    assert any("你好" in message.content_text for message in restored.messages)


def test_gateway_restart_sends_restored_history_to_model(tmp_path):
    db = tmp_path / "memory.db"
    context = MemoryContextLoader(tmp_path / "memory")
    first, _ = _make_gateway(
        [ModelResponse(text="回复1")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=context,
    )
    try:
        first.ask(_event("第一句"), timeout=10)
    finally:
        first.stop()

    second, loop = _make_gateway(
        [ModelResponse(text="回复2")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=context,
    )
    try:
        second.ask(_event("第二句"), timeout=10)
    finally:
        second.stop()

    messages = loop.model.calls[0]
    assert any("第一句" in str(message) for message in messages)
    assert any("回复1" in str(message) for message in messages)


def test_gateway_reset_preserves_searchable_old_history(tmp_path):
    db = tmp_path / "memory.db"
    gateway, _ = _make_gateway(
        [ModelResponse(text="好的")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    try:
        gateway.ask(_event("讨论 staging 部署"), timeout=10)
        gateway.ask(_event("/new"), timeout=10)
    finally:
        gateway.stop()

    scope = build_memory_scope(_event("").source)
    hits = RuntimeStorage.open(db).archive.search_message_candidates(
        scope, "staging", 10
    )
    assert len(hits) == 1
    assert "staging" in hits[0].content_text


def test_sqlite_failure_returns_reply_with_persistence_error(tmp_path):
    class _FailingCommitStore(SqliteSessionStore):
        def commit_turn(self, *args, **kwargs):
            raise RuntimeError("sqlite write failed")

    runtime = RuntimeStorage.open(tmp_path / "agent.db")
    gateway, _ = _make_gateway(
        [ModelResponse(text="模型回复")],
        store=_FailingCommitStore(runtime.db, executions=runtime.executions, jobs=runtime.jobs),
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    try:
        reply = gateway.ask(_event("你好"), timeout=10)
    finally:
        gateway.stop()

    assert reply.text == "模型回复"
    assert reply.metadata.get("persistence_error")


def test_gateway_reset_fires_session_end_after_database_commit(tmp_path):
    db = tmp_path / "memory.db"
    store = RuntimeStorage.open(db).sessions
    key = build_session_key(_event("").source, agent_id="main")
    observed = []

    def on_session_end(closed_session):
        active = RuntimeStorage.open(db).sessions.find(key)
        observed.append((closed_session.session_id, active.session_id if active else None))

    gateway, _ = _make_gateway(
        [ModelResponse(text="ok")],
        store=store,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
        on_session_end=on_session_end,
    )
    try:
        gateway.ask(_event("你好"), timeout=10)
        gateway.ask(_event("/new"), timeout=10)
    finally:
        gateway.stop()

    assert len(observed) == 1
    assert observed[0][0] != observed[0][1]


def test_failed_model_reply_is_recorded_with_its_session_turn(tmp_path):
    db = tmp_path / "agent.db"
    gateway, _ = _make_gateway(
        [RuntimeError("provider unavailable")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    gateway.loop.config = replace(gateway.loop.config, model_retries=0)
    event = _event("hi")
    event.source.reply_to_message_id = "platform-message-1"
    try:
        reply = gateway.ask(event, timeout=10)
    finally:
        gateway.stop()

    runtime = RuntimeStorage.open(db)
    key = build_session_key(event.source, agent_id="main")
    session = runtime.sessions.find(key)
    assert session is not None
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[0].metadata["source_message_id"] == "platform-message-1"
    assert session.messages[1].content_text == reply.text
    assert session.messages[1].status == "failed"
    assert session.messages[1].metadata["origin"] == "gateway_error"
    turn = runtime.executions.get_turn(reply.metadata["turn_id"])
    assert turn is not None
    assert turn["status"] == "failed"
    assert [message["message_id"] for message in turn["messages"]] == [
        message.message_id for message in session.messages
    ]
    conn = connect(db)
    try:
        source_type = conn.execute(
            "SELECT source_type FROM messages WHERE message_id=?",
            (session.messages[1].message_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert source_type == "gateway_generated"


def test_failed_gateway_reply_is_not_sent_as_model_history(tmp_path):
    db = tmp_path / "agent.db"
    gateway, loop = _make_gateway(
        [RuntimeError("provider unavailable"), ModelResponse(text="recovered")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    loop.config = replace(loop.config, model_retries=0)
    try:
        failure = gateway.ask(_event("first"), timeout=10)
        recovery = gateway.ask(_event("second"), timeout=10)
    finally:
        gateway.stop()

    assert recovery.text == "recovered"
    assert all(
        failure.text not in str(message)
        for message in loop.model.calls[1]
    )


def test_pre_model_exception_records_user_and_failure_reply(tmp_path):
    class _FailingContext:
        def freeze(self, *args, **kwargs):
            raise RuntimeError("injected context failure")

    db = tmp_path / "agent.db"
    gateway, _ = _make_gateway(
        [],
        store=RuntimeStorage.open(db).sessions,
        memory_context=_FailingContext(),
    )
    event = _event("hi")
    try:
        reply = gateway.ask(event, timeout=10)
    finally:
        gateway.stop()

    runtime = RuntimeStorage.open(db)
    key = build_session_key(event.source, agent_id="main")
    session = runtime.sessions.find(key)
    assert session is not None
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[0].content_text == "hi"
    assert session.messages[1].content_text == reply.text
    assert session.messages[1].status == "failed"
    turn = runtime.executions.get_turn(reply.metadata["turn_id"])
    assert turn is not None
    assert turn["status"] == "failed"
    assert len(turn["messages"]) == 2


def test_inflight_failure_keeps_original_turn_and_messages(tmp_path):
    class _FailingPrecision:
        def build(self, *args, **kwargs):
            raise RuntimeError("injected precision failure")

    db = tmp_path / "agent.db"
    gateway, loop = _make_gateway(
        [],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    loop.precision = _FailingPrecision()
    event = _event("hi")
    try:
        reply = gateway.ask(event, timeout=10)
    finally:
        gateway.stop()

    runtime = RuntimeStorage.open(db)
    key = build_session_key(event.source, agent_id="main")
    session = runtime.sessions.find(key)
    assert session is not None
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[0].trajectory_id == reply.metadata["turn_id"]
    turn = runtime.executions.get_turn(reply.metadata["turn_id"])
    assert turn is not None
    assert turn["status"] == "failed"
    assert [message["message_id"] for message in turn["messages"]] == [
        message.message_id for message in session.messages
    ]


def test_inflight_failure_preserves_partial_facts_without_model_history(tmp_path):
    class _PartialLoop:
        def run_turn(self, session, user_message, **kwargs):
            user_message.trajectory_id = "partial-turn"
            session.add_message(user_message)
            session.add_message(Message(role="assistant", content_text="partial result"))
            session.tool_calls.append(ToolCall(
                trajectory_id="partial-turn", tool_name="example", status="success",
            ))
            raise RuntimeError("injected tool failure")

    db = tmp_path / "agent.db"
    gateway, _ = _make_gateway(
        [],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    gateway.loop = _PartialLoop()
    event = _event("hi")
    try:
        reply = gateway.ask(event, timeout=10)
    finally:
        gateway.stop()

    runtime = RuntimeStorage.open(db)
    session = runtime.sessions.find(build_session_key(event.source, agent_id="main"))
    assert session is not None
    assert reply.metadata["turn_id"] == "partial-turn"
    assert [message.role for message in session.messages] == ["user", "assistant", "assistant"]
    assert session.messages[1].content_text == "partial result"
    assert session.messages[1].status == "failed"
    assert len(session.tool_calls) == 1
    turn = runtime.executions.get_turn("partial-turn")
    assert turn is not None
    assert len(turn["messages"]) == 3
    assert len(turn["tool_calls"]) == 1

    recovered_gateway, recovered_loop = _make_gateway(
        [ModelResponse(text="recovered")],
        store=RuntimeStorage.open(db).sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
    )
    try:
        recovered_gateway.ask(_event("again"), timeout=10)
    finally:
        recovered_gateway.stop()
    assert all(
        "partial result" not in str(message) and reply.text not in str(message)
        for message in recovered_loop.model.calls[0]
    )
