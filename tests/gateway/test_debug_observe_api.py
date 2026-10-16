"""只读表目录与 trace 模块接口。"""
from __future__ import annotations

import pytest

from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.core.memory.memory_scope import build_memory_scope
from agent.types.memory.trace import MemoryItemTrace, MemoryTrace
from agent.types.memory.trace_categories import MemoryTraceDecision, MemoryTraceOperation
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.types.channel import SessionSource
from agent.core.storage.sqlite.document_registry import SqliteDocumentRegistry
from agent.core.storage.sqlite.session_store import SqliteSessionStore
from agent.core.storage.sqlite.trace_store import SqliteTraceStore
from tests.gateway.test_debug_memory_api import _gateway, _get, _start_server, _stop_server, _turn


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


def _serve(memory_stack, databases, *, enable_memory_debug):
    return _start_server(
        _gateway(memory_stack),
        enable_memory_debug=enable_memory_debug,
        observe_databases=databases,
    )


def test_observe_routes_404_when_flag_off(memory_stack):
    server, port = _serve(memory_stack, {"agent": memory_stack["db"]}, enable_memory_debug=False)
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
    server, port = _serve(
        memory_stack,
        {
            "agent": memory_stack["db"],
            "evolution_audit": audit_path,
        },
        enable_memory_debug=True,
    )
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
        assert {"memory_cards", "memory_experiences", "memory_evidence",
                "memory_terms", "memory_card_versions", "memory_conflicts"} <= session_tables

        status, page = _get(
            port, "/api/debug/tables/rows?database=agent&table=sessions&limit=1&offset=0"
        )
        assert status == 200
        assert page["total"] >= 1
        assert len(page["rows"]) == 1
        assert page["rows"][0]["session_id"] == memory_stack["session"].session_id

        status, filtered = _get(
            port, '/api/debug/tables/rows?database=agent&table=sessions&filters=%7B%22channel%22%3A%22frontier%22%7D'
        )
        assert status == 200
        assert filtered["total"] >= 1
        assert filtered["filter_options"]["channel"] == ["frontier"]
        assert all(row["channel"] == "frontier" for row in filtered["rows"])

        status, rejected = _get(
            port, "/api/debug/tables/rows?database=agent&table=not_a_table"
        )
        assert status == 400
        assert rejected["error"] == "unknown table"
    finally:
        _stop_server(server)


def test_trace_modules_read_perception_note_and_memory_rows(memory_stack):
    session = memory_stack["session"]
    scope = memory_stack["scope"]
    session.add_message(Message(role="user", content_text="perception-row"))
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
            perception_trace={
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
    server, port = _serve(memory_stack, {"agent": memory_stack["db"]}, enable_memory_debug=True)
    try:
        status, modules = _get(port, "/api/debug/traces/modules")
        assert status == 200
        by_module = {item["module"]: item for item in modules["modules"]}
        assert set(by_module) == {"perception", "memory"}
        assert by_module["perception"]["persisted"] is True
        assert "当前进程" in by_module["perception"]["note"]
        assert by_module["memory"]["note"] is None

        status, perception = _get(port, "/api/debug/traces?module=perception")
        assert status == 200
        names = [column["name"] for column in perception["columns"]]
        assert "trace_id" in names
        assert "conversation" in names
        assert "dropped_count" in names
        row = perception["rows"][0]
        assert row["trace_id"] == "t-prec"
        assert "perception-row" in row["conversation"]
        assert "budget" in row["records"]

        status, memory = _get(port, "/api/debug/traces?module=memory")
        assert status == 200
        assert memory["rows"][0]["trace_id"] == "tr-1"
        assert memory["rows"][0]["operation"] == "validate"
        assert "preference" in memory["rows"][0]["items"]

        assert memory["filter_options"]["operation"] == ["validate"]
        status, filtered = _get(
            port, '/api/debug/traces?module=memory&filters=%7B%22operation%22%3A%22validate%22%7D'
        )
        assert status == 200 and filtered["total"] == 1
        assert filtered["rows"][0]["operation"] == "validate"

        status, rejected = _get(port, "/api/debug/traces?module=skill")
        assert status == 400
        assert rejected["error"] == "unknown module"
    finally:
        _stop_server(server)
