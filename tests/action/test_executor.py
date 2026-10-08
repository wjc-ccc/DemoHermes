import time
from concurrent.futures import ThreadPoolExecutor
from agent.action.config import ActionConfig
from agent.action.decision import GateDecision
from agent.action.ledger import now_ms
from agent.action.policy import Toolset
from agent.action.runtime import ActionRuntime
from agent.evolution.assembly import AssemblyPolicy
from agent.model.baseStructure import ToolCallRequest
from conftest import context, payment


def test_pre_rejects_hidden_tools_and_extra_fields(bank_runtime):
    bank, _, runtime = bank_runtime
    ctx = context(runtime)
    bad = payment(kind="forged")
    call = runtime.execute_batch((bad,), ctx)[0]
    assert not call.ok and "invalid_arguments" in call.error
    forged = payment(_scope="another-user")
    assert "internal_argument_forbidden" in runtime.execute_batch((forged,), ctx)[0].error
    missing = ToolCallRequest(id="missing", name="memory_edit", arguments={})
    assert "tool_not_in_toolset" in runtime.execute_batch((missing,), ctx)[0].error
    assert bank.calls == 0


def test_same_business_action_is_deduped_across_turns_and_contract_versions(bank_runtime):
    bank, _, runtime = bank_runtime
    first = runtime.execute_batch((payment(), payment()), context(runtime))
    assert all(call.ok for call in first) and bank.calls == 1
    entry = runtime.catalog.get("fake_pay")
    entry["policy"] = entry["policy"].model_copy(update={"contract_version": "2"})
    replay = runtime.execute_batch((payment(),), context(runtime))[0]
    assert replay.ok and bank.calls == 1
    conflict = runtime.execute_batch((payment(amount=200),), context(runtime))[0]
    assert "idempotency_parameter_conflict" in conflict.error and bank.calls == 1


def test_concurrent_claim_and_resource_conflict(bank_runtime):
    bank, _, runtime = bank_runtime
    bank.delay = 0.08
    contexts = (context(runtime), context(runtime))
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = tuple(pool.map(lambda ctx: runtime.execute_batch((payment(),), ctx)[0], contexts))
    assert bank.calls == 1 and sum(call.ok for call in calls) >= 1


def test_timeout_remains_unknown_until_late_confirmation(bank_runtime):
    bank, _, runtime = bank_runtime
    entry = runtime.catalog.get("fake_pay")
    entry["policy"] = entry["policy"].model_copy(update={"timeout_s": 0.01})
    bank.delay = 0.15
    ctx = context(runtime)
    call = runtime.execute_batch((payment(),), ctx)[0]
    entry_id = call.metadata["action"]["ledger_entry_id"]
    assert "unknown" in call.error and runtime.ledger.get(entry_id).effect_state == "unknown"
    assert not runtime.execute_batch((payment(),), context(runtime))[0].ok
    time.sleep(0.2)
    assert runtime.ledger.get(entry_id).effect_state == "succeeded" and bank.calls == 1


def test_restart_queries_external_success_and_not_found_stays_unknown(bank_runtime):
    bank, storage, runtime = bank_runtime
    bank.lose_response = True
    call = runtime.execute_batch((payment(),), context(runtime))[0]
    item = runtime.ledger.get(call.metadata["action"]["ledger_entry_id"])
    runtime.ledger.update(item, changes={"lease_expires_at": 0}, event_type="crashed")
    restarted = ActionRuntime(storage.db, runtime.catalog, config=runtime.config)
    try:
        assert restarted.recover()[0].effect_state == "succeeded"
        assert restarted.execute_batch((payment(),), context(restarted))[0].ok and bank.calls == 1
    finally:
        restarted.stop()


def test_post_block_preserves_success_and_does_not_pay_again(bank_runtime):
    bank, _, runtime = bank_runtime
    runtime.catalog.get("fake_pay")["post_guards"] = (lambda raw, ctx: GateDecision(
        verdict="block", layer="post", reasons=("receipt_not_publishable",)),)
    first = runtime.execute_batch((payment(),), context(runtime))[0]
    item = runtime.ledger.get(first.metadata["action"]["ledger_entry_id"])
    assert item.status == "blocked_post" and item.effect_state == "succeeded"
    assert not runtime.execute_batch((payment(),), context(runtime))[0].ok
    assert bank.calls == 1


def test_approval_is_bound_to_current_resource_and_pre_runs_again(bank_runtime):
    bank, _, runtime = bank_runtime
    entry = runtime.catalog.get("fake_pay")
    entry["policy"] = entry["policy"].model_copy(update={"requires_approval": True})
    blocked = runtime.execute_batch((payment(),), context(runtime))[0]
    runtime.approve(blocked.metadata["action"]["ledger_entry_id"], actor_id="owner", expires_at=now_ms() + 10000)
    bank.version = "2"
    assert "approval_required" in runtime.execute_batch((payment(),), context(runtime))[0].error
    assert bank.calls == 0


def test_contract_drift_and_call_budget(bank_runtime):
    bank, _, runtime = bank_runtime
    ctx = context(runtime)
    entry = runtime.catalog.get("fake_pay")
    entry["policy"] = entry["policy"].model_copy(update={"implementation_version": "2"})
    assert "contract_drift" in runtime.execute_batch((payment(),), ctx)[0].error
    runtime.toolsets["default"] = Toolset(tools=runtime.catalog.names(), max_risk="high", max_calls={"fake_pay": 1})
    ctx = context(runtime)
    calls = runtime.execute_batch((payment(), payment(obligation_id="E0012")), ctx)
    assert calls[0].ok and "call_budget_exhausted" in calls[1].error


def test_replay_applies_new_post_policy_without_new_side_effect(bank_runtime):
    bank, _, runtime = bank_runtime
    assert runtime.execute_batch((payment(),), context(runtime))[0].ok
    def redact(raw, ctx):
        output = raw.model_dump(mode="json")
        output["data"] = {"amount": raw.data["amount"]}
        output["result_text"] = "已付款，回执已遮蔽"
        return GateDecision(verdict="accept", layer="post", effective_value=output)
    runtime.catalog.get("fake_pay")["post_guards"] = (redact,)
    replay = runtime.execute_batch((payment(),), context(runtime))[0]
    item = runtime.ledger.get(replay.metadata["action"]["ledger_entry_id"])
    assert replay.ok and "external_ref" not in replay.tool_call_result
    assert "external_ref" in item.raw_output["data"] and item.publish_state == "redacted" and bank.calls == 1


def test_not_found_does_not_authorize_a_retry(bank_runtime):
    bank, _, runtime = bank_runtime
    bank.lose_response = True
    call = runtime.execute_batch((payment(),), context(runtime))[0]
    item = runtime.ledger.get(call.metadata["action"]["ledger_entry_id"])
    runtime.ledger.update(item, changes={"lease_expires_at": 0}, event_type="crashed")
    bank.receipts.clear()
    assert runtime.recover()[0].effect_state == "unknown"
    assert not runtime.execute_batch((payment(),), context(runtime))[0].ok and bank.calls == 1


def test_pre_records_requested_and_effective_arguments_separately(bank_runtime):
    bank, _, runtime = bank_runtime
    def normalize(values, ctx):
        return GateDecision(verdict="accept", layer="pre", effective_value={**values, "amount": min(values["amount"], 100)})
    runtime.catalog.get("fake_pay")["pre_guards"] = (normalize,)
    call = runtime.execute_batch((payment(amount=105),), context(runtime))[0]
    item = runtime.ledger.get(call.metadata["action"]["ledger_entry_id"])
    assert call.ok and item.requested_args["amount"] == 105 and item.effective_args["amount"] == 100
    assert next(iter(bank.receipts.values()))["amount"] == 100

