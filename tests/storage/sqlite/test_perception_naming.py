"""验证感知模块命名迁移保留完整事实、调试可读性与重复提交语义。"""
import json
import shutil

import pytest

from agent.gateway.trace_modules import list_modules, list_traces
from agent.storage.sqlite import migrations
from agent.storage.sqlite.errors import AuditConflictError
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord


OLD_TRACE = {
    "metrics": {"trace_id": "turn-1", "tokens_used": 12, "anchor_status": "created"},
    "builds": [{"iteration": 1, "messages": [{"role": "user", "content": "percision precision 原文"}],
                "model_calls": [{"usage": {"prompt_tokens": 12, "completion_tokens": 5}}]}],
    "input_token_count": 12,
    "output_token_count": 5,
}
NEW_TRACE = {**OLD_TRACE, "metrics": {**OLD_TRACE["metrics"], "tokens_used": 24}}


@pytest.mark.parametrize("fields,expected", [
    ({}, None),
    ({"precision_trace": None}, None),
    ({"precision_trace": OLD_TRACE}, OLD_TRACE),
    ({"precision_trace": OLD_TRACE, "perception_trace": NEW_TRACE}, NEW_TRACE),
    ({"precision_trace": OLD_TRACE, "perception_trace": None}, None),
])
def test_010_upgrade_preserves_trace_and_reads_only_perception(tmp_path, monkeypatch, fields, expected):
    schema = migrations._SCHEMA_DIR
    legacy_schema = tmp_path / "legacy-schema"
    legacy_schema.mkdir()
    for path in schema.glob("*.sql"):
        if path.stem != "011_perception_naming":
            shutil.copyfile(path, legacy_schema / path.name)
    database = tmp_path / "agent.db"
    with monkeypatch.context() as context:
        context.setattr(migrations, "_SCHEMA_DIR", legacy_schema)
        storage = RuntimeStorage.open(database, id_factory=lambda: "session-1")
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    session = storage.sessions.get_or_create("slot-1", scope=scope, channel="cli", author_id="user-1")
    user = session.add_message(Message(message_id="message-1", role="user", content_text="percision precision 原文"))
    turn = TurnRecord(turn_id="turn-1", session_id=session.session_id, scope=scope,
                      user_message_id=user.message_id, assistant_message_ids=(), tool_call_ids=(),
                      started_at=1, finished_at=2, completed=True, interrupted=False, error=None,
                      input_token_count=12, output_token_count=5)
    storage.sessions.commit_turn(session, turn, scope=scope)
    original = turn.model_dump(mode="json")
    original.pop("perception_trace")
    original.update(fields)
    with storage.db.write() as connection:
        connection.execute("UPDATE turns SET turn_json=? WHERE turn_id=?", (json.dumps(original), turn.turn_id))

    upgraded = RuntimeStorage.open(database)
    RuntimeStorage.open(database)
    with upgraded.db.read() as connection:
        payload = json.loads(connection.execute("SELECT turn_json FROM turns WHERE turn_id=?", (turn.turn_id,)).fetchone()[0])
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='011_perception_naming'").fetchone()[0] == 1
        assert connection.execute("SELECT content_text FROM messages WHERE message_id=?", (user.message_id,)).fetchone()[0] == "percision precision 原文"
    assert "precision_trace" not in payload
    expected_payload = {key: value for key, value in original.items() if key != "precision_trace"}
    if "precision_trace" in fields and "perception_trace" not in fields:
        expected_payload["perception_trace"] = expected
    assert payload == expected_payload
    assert upgraded.executions.get_record(turn.turn_id).perception_trace == expected
    assert upgraded.executions.get_turn(turn.turn_id)["perception_trace"] == expected
    modules = {item["module"]: item for item in list_modules()["modules"]}
    assert modules["perception"]["label"] == "Perception"
    assert "precision" not in modules
    traces = list_traces({"agent": database}, "perception", session_id=session.session_id, limit=10, offset=0)
    assert traces["total"] == (1 if expected is not None else 0)
    if expected is not None:
        assert traces["rows"][0]["tokens_used"] == expected["metrics"]["tokens_used"]
    if "perception_trace" in payload:
        restored = upgraded.executions.get_record(turn.turn_id)
        upgraded.sessions.commit_turn(session, restored, scope=scope)
        with pytest.raises(AuditConflictError, match="Turn payload"):
            upgraded.sessions.commit_turn(session, restored.model_copy(update={"error": "changed"}), scope=scope)
