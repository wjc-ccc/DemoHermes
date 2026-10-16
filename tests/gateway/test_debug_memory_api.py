"""Debug memory HTTP API — flag 关 404，开则只读当前 frontier 身份。"""
from __future__ import annotations

import json
from http.client import HTTPConnection
from threading import Thread

import pytest

from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.channel.channel_frontier import FrontierChannel
from tests.loop_fakes import make_loop
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_scope import build_memory_scope, ensure_scope_dir
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.types.channel import SessionSource
from agent.core.gateway.gateway import Gateway
from agent.core.gateway.http_api import FrontierHttpServer
from agent.core.gateway.session_key import build_session_key
from agent.core.storage.sqlite.document_registry import SqliteDocumentRegistry
from agent.core.storage.sqlite.session_store import SqliteSessionStore


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
    session_key = build_session_key(source)
    session = store.get_or_create(
        session_key, scope=scope, channel="frontier", author_id="web-user"
    )
    session.add_message(Message(role="user", content_text="debug-me"))
    store.commit_turn(session, _turn(session, scope), scope=scope)
    return {
        "store": store,
        "memory_context": memory_context,
        "session": session,
        "session_key": session_key,
        "scope": scope,
        "root": root,
    }


class _StubModel:
    name = "stub"

    def chat(self, messages, tools=None, **kwargs):
        raise AssertionError("debug API 不应调用模型")


def _gateway(memory_stack) -> Gateway:
    gateway = Gateway(
        loop=make_loop(model=_StubModel()),
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
    )
    gateway.register_channel(FrontierChannel())
    return gateway


def _start_server(gateway, *, enable_memory_debug: bool, **options):
    port = _free_port()
    server = FrontierHttpServer(
        gateway, port=port, enable_memory_debug=enable_memory_debug, **options
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _stop_server(server):
    server.shutdown()
    server.server_close()


def _request(port, method, path, *, headers=None, body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=payload, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    result = resp.status, json.loads(raw) if raw else {}, dict(resp.getheaders())
    conn.close()
    return result


def _get(port, path):
    status, body, _ = _request(port, "GET", path)
    return status, body


def test_perception_context_persists_iterations_and_obeys_debug_auth(memory_stack):
    from agent.core.perception.perception_trace import PerceptionTrace
    from agent.types.perception.result import PerceptionMetrics, PerceptionResult

    session = memory_stack["session"]
    session.add_message(Message(role="user", content_text="snapshot user"))
    trace = PerceptionTrace()
    text = "HEAD-" + "完整内容" * 3000 + "-TAIL"
    result = PerceptionResult(messages=[{"role": "system", "content": "first"}],
                             metrics=PerceptionMetrics(trace_id="t-full"))
    trace.record(result, iteration=1)
    result = result.model_copy(update={"messages": [{"role": "user", "content": text}]})
    trace.record(result, iteration=2)
    tools = [{"function": {"name": "test_tool", "description": "完整定义"}}]
    trace.record_request("t-full", 2, tools)
    trace.record_failure("t-full", 3, "context_budget_exceeded")
    turn = _turn(session, memory_stack["scope"]).model_copy(update={
        "turn_id": "t-full", "perception_trace": trace.get("t-full").model_dump(mode="json")})
    memory_stack["store"].commit_turn(session, turn, scope=memory_stack["scope"])
    # 使用新 Gateway 的空进程窗口，验证实际读取的是持久化全文。
    gateway = _gateway(memory_stack)
    gateway.loop.trace.perception = PerceptionTrace()
    server, port = _start_server(gateway, enable_memory_debug=True, api_token="test-token")
    try:
        path = "/api/debug/memory/trace?turn_id=t-full&iteration=2"
        assert _get(port, path)[0] == 401
        status, body, _ = _request(port, "GET", path, headers={"Authorization": "Bearer test-token"})
        assert status == 200
        assert body["build"]["messages"][0]["content"] == text
        assert body["build"]["tools"] == tools
        assert len(body["iterations"]) == 3
        assert "builds" not in body["trace"]
        _, failed, _ = _request(port, "GET", "/api/debug/memory/trace?turn_id=t-full&iteration=3",
                               headers={"Authorization": "Bearer test-token"})
        assert failed["unavailable_reason"] == "context_build_failed"
        assert failed["build"]["messages"] is None
        assert _request(port, "GET", "/api/debug/memory/trace?turn_id=t-full&iteration=0",
                        headers={"Authorization": "Bearer test-token"})[0] == 400
        _, legacy, _ = _request(port, "GET", "/api/debug/memory/trace?turn_id=t-debug&iteration=1",
                               headers={"Authorization": "Bearer test-token"})
        assert legacy["unavailable_reason"] == "full_context_not_saved"
        assert legacy["build"] is None
    finally:
        _stop_server(server)
    server, port = _start_server(gateway, enable_memory_debug=False)
    try:
        assert _get(port, "/api/debug/memory/trace?turn_id=t-full&iteration=2")[0] == 404
    finally:
        _stop_server(server)


def test_debug_session_404_when_flag_off(memory_stack):
    gateway = _gateway(memory_stack)
    server, port = _start_server(gateway, enable_memory_debug=False)
    try:
        status, body = _get(port, "/api/debug/memory/session?chat_id=web&user_id=web-user")
        assert status == 404
        assert "error" in body
    finally:
        _stop_server(server)


def test_debug_session_returns_messages_when_flag_on(memory_stack):
    gateway = _gateway(memory_stack)
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, body = _get(port, "/api/debug/memory/session?chat_id=web&user_id=web-user")
        assert status == 200
        assert body["session_key"] == memory_stack["session_key"]
        assert body["session_id"] == memory_stack["session"].session_id
        assert any(m.get("content_text") == "debug-me" for m in body["messages"])
    finally:
        _stop_server(server)


def test_debug_context_returns_frozen_snapshot_and_revisions(memory_stack):
    scope_dir = ensure_scope_dir(memory_stack["root"], memory_stack["scope"])
    (scope_dir / "MEMORY.md").write_text(
        "# MEMORY\n用户偏好中文\n", encoding="utf-8"
    )

    gateway = _gateway(memory_stack)
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
    gateway = _gateway(memory_stack)
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
        assert detail["perception_trace"] is None
    finally:
        _stop_server(server)
