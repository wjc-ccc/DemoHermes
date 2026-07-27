"""Slice 14: Gateway 端到端接线与失败（§8.1/§8.3）。

公共 seam：Gateway + MemoryManager + SessionStore + AgentLoop。
Fake 仅用于模型与 Provider；Gateway/Manager/ContextBuilder/SQLite/文件系统用真实实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Agent.channel.channel_cli import CliChannel
from Agent.core.events import EventBus
from Agent.core.loop import AgentLoop
from Agent.core.types import InboundEvent, SessionSource
from Agent.gateway import Gateway, build_session_key
from Agent.memory import (
    BuiltinMemoryMutation,
    MemoryManager,
    MemoryProvenance,
    MemorySearchRequest,
    build_memory_scope,
)
from Agent.memory.base import MemoryProvider
from Agent.memory.episodic.store import SqliteSessionArchiveReader, SqliteSessionStore
from Agent.memory.types import ProviderContribution, ProviderRecallRequest
from Agent.model.baseStructure import ModelResponse


class _MockModel:
    """按脚本返回的假模型；记录每次收到的 messages 供断言。"""

    name = "mock"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _BoomProvider(MemoryProvider):
    """recall 总是抛错，用于验证 Gateway 不因 provider 失败而丢回复（§14.6）。"""

    name = "boom"

    def recall(self, request: ProviderRecallRequest) -> ProviderContribution:
        raise RuntimeError("provider down")


def _event(text: str) -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(channel="cli", chat_id="local", chat_type="dm", user_id="u1"),
    )


_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="t_001")


def _make_gateway(script, *, store, memory, on_session_end=None):
    loop = AgentLoop(model=_MockModel(script), event_bus=EventBus())
    gw = Gateway(
        loop=loop, store=store, memory=memory, workers=1, on_session_end=on_session_end
    )
    gw.register_channel(CliChannel(quiet=True))
    gw.start()
    return gw, loop


def test_gateway_turn_builds_memory_once_and_persists_session(tmp_path):
    """§8.1：Gateway 每 turn build_context 一次注入模型，并经 commit_turn 持久化 session。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    scope = build_memory_scope(_event("你好").source)
    manager.apply(
        BuiltinMemoryMutation(
            scope=scope, target="profile", action="add",
            content="用户偏好简洁", provenance=_PROVENANCE,
        )
    )

    gw, loop = _make_gateway([ModelResponse(text="收到")], store=store, memory=manager)
    try:
        reply = gw.ask(_event("你好"), timeout=10)
    finally:
        gw.stop()

    assert reply.text == "收到"
    # 模型收到了 memory_context（含 profile 偏好与 memory 围栏）
    msgs = loop.model.calls[0]["messages"]
    assert any("<memory-context>" in str(m) for m in msgs)
    assert any("用户偏好简洁" in str(m) for m in msgs)
    # session 已持久化：新 store 实例可恢复，含本次 user 消息
    key = build_session_key(_event("你好").source, agent_id="main")
    restored = SqliteSessionStore(db).find(key)
    assert restored is not None
    assert any("你好" in m.content_text for m in restored.messages)


def test_gateway_restart_second_request_sends_restored_history_to_model(tmp_path):
    """§14.2：重启（新 store 实例）后第二请求把恢复的历史发给模型。"""
    db = tmp_path / "memory.db"
    manager = MemoryManager(root=tmp_path / "memory")

    gw1, loop1 = _make_gateway(
        [ModelResponse(text="回复1")], store=SqliteSessionStore(db), memory=manager
    )
    try:
        gw1.ask(_event("第一句"), timeout=10)
    finally:
        gw1.stop()

    # 重启：新 store 实例 + 新 gateway，复用同一 db
    gw2, loop2 = _make_gateway(
        [ModelResponse(text="回复2")], store=SqliteSessionStore(db), memory=manager
    )
    try:
        gw2.ask(_event("第二句"), timeout=10)
    finally:
        gw2.stop()

    msgs = loop2.model.calls[0]["messages"]
    assert any("第一句" in str(m) for m in msgs)  # 恢复的历史
    assert any("回复1" in str(m) for m in msgs)


def test_gateway_reset_preserves_searchable_old_history(tmp_path):
    """§14.3：reset 后旧 transcript 永久保留且可按原 scope 搜索。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    gw, _ = _make_gateway([ModelResponse(text="好的")], store=store, memory=manager)
    try:
        gw.ask(_event("讨论 staging 部署"), timeout=10)
        gw.ask(_event("/new"), timeout=10)  # reset：关闭旧 session、建新 session
    finally:
        gw.stop()

    scope = build_memory_scope(_event("").source)
    hits = manager.search(
        MemorySearchRequest(scope=scope, query="staging", sources=("session",))
    )
    assert len(hits) == 1
    assert "staging" in hits[0].snippet


def test_provider_failure_still_returns_model_reply(tmp_path):
    """§14.6：provider recall 失败仍返回模型回复（build_context 降级，不阻断主对话）。"""
    db = tmp_path / "memory.db"
    manager = MemoryManager(root=tmp_path / "memory", provider=_BoomProvider())

    gw, _ = _make_gateway(
        [ModelResponse(text="模型回复")], store=SqliteSessionStore(db), memory=manager
    )
    try:
        reply = gw.ask(_event("你好"), timeout=10)
    finally:
        gw.stop()

    assert reply.text == "模型回复"


def test_sqlite_failure_returns_reply_with_persistence_error(tmp_path):
    """§14.5：commit_turn 失败时不丢模型回复，reply 带 persistence_error。"""
    db = tmp_path / "memory.db"

    class _FailingCommitStore(SqliteSessionStore):
        def commit_turn(self, *a, **kw):
            raise RuntimeError("sqlite write failed")

    store = _FailingCommitStore(db)
    manager = MemoryManager(root=tmp_path / "memory")

    gw, _ = _make_gateway(
        [ModelResponse(text="模型回复")], store=store, memory=manager
    )
    try:
        reply = gw.ask(_event("你好"), timeout=10)
    finally:
        gw.stop()

    assert reply.text == "模型回复"  # 不丢回复
    assert reply.metadata.get("persistence_error")  # 标记持久化失败


def test_gateway_reset_fires_session_end_only_after_database_commit(tmp_path):
    """§8.4：reset 的 session_end hook 在 db commit 之后才触发（hook 触发时旧 session 已非 active）。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    key = build_session_key(_event("").source, agent_id="main")

    hook_observed: list = []

    def on_session_end(closed_session):
        # hook 触发时查 active：commit 完成则 active 已切到新 session（≠ closed）
        active = SqliteSessionStore(db).find(key)
        hook_observed.append((closed_session.session_id, active.session_id if active else None))

    gw, _ = _make_gateway(
        [ModelResponse(text="ok")], store=store, memory=manager, on_session_end=on_session_end
    )
    try:
        gw.ask(_event("你好"), timeout=10)
        gw.ask(_event("/new"), timeout=10)
    finally:
        gw.stop()

    assert len(hook_observed) == 1
    closed_id, active_id = hook_observed[0]
    assert closed_id != active_id  # hook 触发时 active 已是新 session，证明 commit 已完成


def test_memory_tool_never_exposes_absolute_storage_path(tmp_path):
    """§14.7：memory 工具返回不含绝对路径 / SQL / 物理文件名。"""
    from Agent.tools.memory_tools import MemoryReadTool

    manager = MemoryManager(root=tmp_path / "memory")
    scope = build_memory_scope(_event("").source)
    manager.apply(
        BuiltinMemoryMutation(
            scope=scope, target="profile", action="add",
            content="偏好简洁回答", provenance=_PROVENANCE,
        )
    )

    tool = MemoryReadTool(manager)
    result = tool.execute({"_scope": scope, "target": "profile"})

    assert result.ok
    assert "偏好简洁回答" in result.result_text
    # 不暴露绝对路径 / SQL / 物理文件名
    assert str(tmp_path) not in result.result_text
    assert "SELECT" not in result.result_text
    assert "USER.md" not in result.result_text
