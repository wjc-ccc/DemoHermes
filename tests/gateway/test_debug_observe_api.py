"""只读表目录与 trace 模块接口。"""
from __future__ import annotations

import pytest

from agent.storage.sqlite import RuntimeStorage
from agent.channel.channel_frontier import FrontierChannel
from agent.core.memory import MemoryContextLoader, build_memory_scope
from agent.core.memory.memory_trace import (
    MemoryItemTrace,
    MemoryTrace,
    MemoryTraceDecision,
    MemoryTraceOperation,
)
from agent.core.memory.types import TurnRecord
from agent.core.types import Message, SessionSource
from agent.gateway.gateway import Gateway
from agent.storage.sqlite import SqliteDocumentRegistry, SqliteSessionStore, SqliteTraceStore
from tests.gateway.test_debug_memory_api import _get, _start_server, _stop_server, _turn


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
    session = store.get_or_create(
        "agent:main:frontier:dm:web",
        scope=scope,
        channel="frontier",
        author_id="web-user",
    )
    session.add_message(Message(role="user", content_text="debug-me"))
    store.commit_turn(session, _turn(session, scope), scope=scope)
    return {
        "store": store,
        "memory_context": memory_context,
        "session": session,
        "scope": scope,
        "db": db,
    }


def _gateway(memory_stack, databases):
    gateway = Gateway(
        store=memory_stack["store"],
        memory_context=memory_stack["memory_context"],
        observe_databases=databases,
    )
    gateway.register_channel(FrontierChannel())
    return gateway


def test_observe_routes_404_when_flag_off(memory_stack):
    gateway = _gateway(memory_stack, {"agent": memory_stack["db"]})
    server, port = _start_server(gateway, enable_memory_debug=False)
    try:
        status, body = _get(port, "/api/debug/tables")
        assert status == 404
        assert "error" in body
        status, body = _get(port, "/api/debug/traces/modules")
        assert status == 404
    finally:
        _stop_server(server)


def test_table_catalog_lists_sessions_and_side_database(memory_stack, tmp_path):
    audit_path = tmp_path / "evolution_audit.db"
    RuntimeStorage.open(audit_path).audit
    gateway = _gateway(
        memory_stack,
        {
            "agent": memory_stack["db"],
            "evolution_audit": audit_path,
        },
    )
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, body = _get(port, "/api/debug/tables")
        assert status == 200
        by_name = {item["name"]: item for item in body["databases"]}
        session_tables = {table["name"] for table in by_name["agent"]["tables"]}
        assert "sessions" in session_tables
        assert "turns" in session_tables
        session_columns = next(
            table["columns"]
            for table in by_name["agent"]["tables"]
            if table["name"] == "sessions"
        )
        assert any(column["name"] == "session_id" and column["pk"] for column in session_columns)
        assert "audit_events" in {table["name"] for table in by_name["agent"]["tables"]}
        assert len(by_name["agent"]["tables"]) == 17

        status, page = _get(
            port, "/api/debug/tables/rows?database=agent&table=sessions&limit=1&offset=0"
        )
        assert status == 200
        assert page["total"] >= 1
        assert len(page["rows"]) == 1
        assert page["rows"][0]["session_id"] == memory_stack["session"].session_id

        status, rejected = _get(
            port, "/api/debug/tables/rows?database=agent&table=not_a_table"
        )
        assert status == 400
        assert rejected["error"] == "unknown table"
    finally:
        _stop_server(server)


def test_trace_modules_read_precision_note_and_memory_rows(memory_stack):
    session = memory_stack["session"]
    scope = memory_stack["scope"]
    session.add_message(Message(role="user", content_text="precision-row"))
    memory_stack["store"].commit_turn(
        session,
        TurnRecord(
            turn_id="t-prec",
            session_id=session.session_id,
            scope=scope,
            user_message_id=session.messages[-1].message_id,
            assistant_message_ids=(),
            tool_call_ids=(),
            started_at=1002.0,
            finished_at=1003.0,
            completed=True,
            interrupted=False,
            error=None,
            precision_trace={
                "metrics": {
                    "trace_id": "t-prec",
                    "budget": 100,
                    "tokens_used": 10,
                    "budget_usage": 0.1,
                    "dropped_count": 2,
                    "message_count": 1,
                },
                "records": [
                    {
                        "item_name": "history:1",
                        "priority": 2,
                        "decision": "dropped",
                        "reason": "budget",
                    }
                ],
            },
        ),
        scope=scope,
    )
    RuntimeStorage.open(memory_stack["db"]).traces.put(
        MemoryTrace(
            trace_id="tr-1",
            operation=MemoryTraceOperation.VALIDATE,
            session_id=session.session_id,
            turn_id="t-prec",
            items=(
                MemoryItemTrace(
                    trace_item_id="ti-1",
                    decision=MemoryTraceDecision.ACCEPTED,
                    type="preference",
                ),
            ),
        )
    )
    gateway = _gateway(memory_stack, {"agent": memory_stack["db"]})
    server, port = _start_server(gateway, enable_memory_debug=True)
    try:
        status, modules = _get(port, "/api/debug/traces/modules")
        assert status == 200
        by_module = {item["module"]: item for item in modules["modules"]}
        assert set(by_module) == {"precision", "memory"}
        assert by_module["precision"]["persisted"] is True
        assert "当前进程" in by_module["precision"]["note"]
        assert by_module["memory"]["note"] is None

        status, precision = _get(port, "/api/debug/traces?module=precision")
        assert status == 200
        names = [column["name"] for column in precision["columns"]]
        assert "trace_id" in names
        assert "conversation" in names
        assert "dropped_count" in names
        row = precision["rows"][0]
        assert row["trace_id"] == "t-prec"
        assert "precision-row" in row["conversation"]
        assert "budget" in row["records"]

        status, memory = _get(port, "/api/debug/traces?module=memory")
        assert status == 200
        assert memory["rows"][0]["trace_id"] == "tr-1"
        assert memory["rows"][0]["operation"] == "validate"
        assert "preference" in memory["rows"][0]["items"]

        status, rejected = _get(port, "/api/debug/traces?module=skill")
        assert status == 400
        assert rejected["error"] == "unknown module"
    finally:
        _stop_server(server)

def test_three_state_endpoint_is_readonly_and_flag_guarded(memory_stack):
    gateway = _gateway(memory_stack, {"agent":memory_stack["db"]})
    sid = memory_stack["session"].session_id
    server,port = _start_server(gateway,enable_memory_debug=True)
    try:
        with gateway.store.db.read() as conn:
            before=tuple(conn.iterdump())
        status,payload=_get(port,"/api/debug/memory/states?session_id="+sid)
        assert status==200 and payload["session_id"]==sid
        assert payload["workspace"] is None
        assert payload["state"]["items"]==[]
        with gateway.store.db.read() as conn:
            assert tuple(conn.iterdump())==before
        assert _get(port,"/api/debug/memory/states?session_id=missing")[0]==404
    finally:
        _stop_server(server)
    server,port=_start_server(gateway,enable_memory_debug=False)
    try:
        assert _get(port,"/api/debug/memory/states?session_id="+sid)[0]==404
    finally:
        _stop_server(server)
