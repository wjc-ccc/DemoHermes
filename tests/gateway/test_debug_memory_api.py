"""Debug memory HTTP API — flag 关 404，开则只读当前 frontier 身份。"""
from __future__ import annotations

import json
from http.client import HTTPConnection
from threading import Thread

import pytest

from agent.storage.sqlite import RuntimeStorage
from agent.channel.channel_frontier import FrontierChannel
from agent.core.memory import MemoryContextLoader, MemoryScope, build_memory_scope, ensure_scope_dir
from agent.core.memory.types import TurnRecord
from agent.core.types import Message
from agent.core.types import SessionSource
from agent.gateway.gateway import Gateway
from agent.gateway.http_api import FrontierHttpServer
from agent.storage.sqlite import SqliteDocumentRegistry, SqliteSessionStore


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _turn(session, scope):
    return TurnRecord(
        turn_id="t-debug",
        session_id=session.session_id,
        scope=scope,
        user_message_id=session.messages[-1].message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


@pytest.fixture
def memory_stack(tmp_path):
    db = tmp_path / "memory.db"
    root = tmp_path / "memory"
    store = RuntimeStorage.open(db).sessions
    memory_context = MemoryContextLoader(
        root=root,
        document_registry=RuntimeStorage.open(db).documents,
    )
    source = SessionSource(
        channel="frontier", chat_id="web", chat_type="dm", user_id="web-user"
    )
    scope = build_memory_scope(source)
    session_key = "agent:main:frontier:dm:web"
    session = store.get_or_create(
        session_key, scope=scope, channel="frontier", author_id="web-user"
    )
    session.add_message(Message(role="user", content_text="debug-me"))
    store.commit_turn(session, _turn(session, scope), scope=scope)
    return {
        "store": store,
        "memory_context": memory_context,
        "session": session,
        "scope": scope,
        "root": root,
    }


def _start_server(gateway, *, enable_memory_debug: bool):
    port = _free_port()
    server = FrontierHttpServer(
        gateway, port=port, enable_memory_debug=enable_memory_debug
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _stop_server(server):
    server.shutdown()
    server.server_close()


def _get(port, path):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(body) if body else {}


def test_debug_session_404_when_flag_off(memory_stack):
    gateway = Gateway(
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
    )
    gateway.register_channel(FrontierChannel())
    server, port = _start_server(gateway, enable_memory_debug=False)
    try:
        status, body = _get(port, "/api/debug/memory/session?chat_id=web&user_id=web-user")
        assert status == 404
        assert "error" in body
    finally:
        _stop_server(server)


def test_debug_session_returns_messages_when_flag_on(memory_stack):
    gateway = Gateway(
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
    )
    gateway.register_channel(FrontierChannel())
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, body = _get(port, "/api/debug/memory/session?chat_id=web&user_id=web-user")
        assert status == 200
        assert body["session_key"] == "agent:main:frontier:dm:web"
        assert body["session_id"] == memory_stack["session"].session_id
        assert any(m.get("content_text") == "debug-me" for m in body["messages"])
    finally:
        _stop_server(server)


def test_debug_context_returns_frozen_snapshot_and_revisions(memory_stack):
    scope_dir = ensure_scope_dir(memory_stack["root"], memory_stack["scope"])
    (scope_dir / "MEMORY.md").write_text(
        "# MEMORY\n用户偏好中文\n", encoding="utf-8"
    )

    gateway = Gateway(
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
    )
    gateway.register_channel(FrontierChannel())
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, body = _get(
            port, "/api/debug/memory/context?chat_id=web&user_id=web-user"
        )
        assert status == 200
        assert "用户偏好中文" in body["snapshot"]["prompt"]
        assert body["snapshot"]["revisions"][0]["layer"] == "user"
    finally:
        _stop_server(server)


def test_debug_sessions_turns_and_turn_detail_are_queryable(memory_stack):
    gateway = Gateway(
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
    )
    gateway.register_channel(FrontierChannel())
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, sessions = _get(
            port, "/api/debug/memory/sessions?chat_id=web&user_id=web-user"
        )
        assert status == 200
        assert sessions["sessions"][0]["session_id"] == memory_stack["session"].session_id

        session_id = memory_stack["session"].session_id
        status, turns = _get(port, f"/api/debug/memory/turns?session_id={session_id}")
        assert status == 200
        assert turns["turns"][0]["turn_id"] == "t-debug"

        status, detail = _get(port, "/api/debug/memory/turn?turn_id=t-debug")
        assert status == 200
        assert detail["messages"][0]["content_text"] == "debug-me"
        assert detail["precision_trace"] is None
    finally:
        _stop_server(server)
