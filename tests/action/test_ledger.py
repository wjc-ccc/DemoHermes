import pytest
from agent.types.action.execution import ToolContext, ActionItem, digest
from agent.types.action.policy import Toolset
from agent.core.action.storage.migrations import schema_directory
from agent.core.action.storage.tool_store import SqliteToolLedgerStore, ActionConflictError
from agent.types.model import ToolCallRequest
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def test_ledger_claims_are_persistent_and_success_cannot_be_reset(tmp_path):
    path = tmp_path / "agent.db"
    storage = RuntimeStorage.open(path, extra_schema_dirs=(schema_directory(),))
    ledger = SqliteToolLedgerStore(storage.db)
    context = ToolContext(scope_key="u", session_id="s", turn_id="t", source_message_id="m", toolset=Toolset())
    req = ToolCallRequest(id="call", name="pay", arguments={"obligation": "E0007"})
    item = ActionItem(entry_id="a", idempotency_key="pay:E0007", tool_name="pay", scope_key="u",
                      effect="external", input_hash=digest(req.arguments), resource_ref="payroll:E0007")
    attempt = ledger.begin_attempt(req, context)
    claimed, owner = ledger.reserve(item, attempt, max_calls=8, max_attempts=3, lease_ms=10)
    assert owner and claimed.effect_state == "started"
    second = SqliteToolLedgerStore(RuntimeStorage.open(path).db)
    other = second.begin_attempt(req, context)
    in_flight, owner = second.reserve(item, other, max_calls=8, max_attempts=3, lease_ms=10)
    assert not owner and in_flight.entry_id == claimed.entry_id
    succeeded = ledger.update(claimed, changes={"effect_state": "succeeded"}, event_type="confirmed")
    with pytest.raises(ActionConflictError, match="succeeded_is_immutable"):
        ledger.update(succeeded, changes={"effect_state": "failed"}, event_type="reset")
    blocked = ledger.update(succeeded, changes={"publish_state": "blocked"}, event_type="post")
    assert blocked.status == "blocked_post" and blocked.effect_state == "succeeded"
    assert len(ledger.attempts("a")) == 2


def test_optional_migrations_do_not_change_old_checksums(tmp_path):
    path = tmp_path / "agent.db"
    old = RuntimeStorage.open(path)
    with old.db.read() as conn:
        before = dict(conn.execute("SELECT version,checksum FROM schema_migrations"))
    RuntimeStorage.open(path, extra_schema_dirs=(schema_directory(),))
    reopened = RuntimeStorage.open(path)
    with reopened.db.read() as conn:
        after = dict(conn.execute("SELECT version,checksum FROM schema_migrations"))
    assert all(after[key] == value for key, value in before.items())
    assert "007_action_ledger" in after
