"""Gateway、冻结 MemorySnapshot 与 SQLite Session 的端到端接线。"""
from agent.storage.sqlite import RuntimeStorage
from agent.channel.channel_cli import CliChannel
from agent.core.agent_loop import AgentLoop
from agent.core.event_bus import EventBus
from agent.core.memory import MemoryContextLoader, build_memory_scope, ensure_scope_dir
from agent.core.types import InboundEvent, SessionSource
from agent.gateway import Gateway, build_session_key
from agent.model.baseStructure import ModelResponse
from agent.storage.sqlite import SqliteSessionArchiveReader, SqliteSessionStore


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
    loop = AgentLoop(model=_MockModel(script), event_bus=EventBus())
    gateway = Gateway(
        loop=loop,
        store=store,
        memory_context=memory_context,
        workers=1,
        on_session_end=on_session_end,
    )
    gateway.register_channel(CliChannel(quiet=True))
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
