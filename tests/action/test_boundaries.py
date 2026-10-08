import pytest
import time
from agent.action.decision import GateDecision
from agent.action.plan.plan import Plan, PlanStep, PlanPatch
from agent.action.ledger import now_ms
from agent.model.baseStructure import ToolCallRequest
from agent.evolution.assembly import AssemblyPolicy
from agent.core.types import Session, Message
from agent.core.building_messages import build_tool_message
from agent.action.config import ActionConfig
from agent.action.policy import Toolset, ToolPolicy
from agent.action.runtime import ActionRuntime
from agent.action.adapters.builtin import BuiltinToolAdapter
from agent.tools.base import Tool, ToolCallResult
from conftest import context, payment


def test_distinct_actions_cannot_bypass_unknown_resource_claim(bank_runtime):
    bank, _, runtime = bank_runtime
    adapter = runtime.catalog.get("fake_pay")["adapter"]
    adapter.resource = lambda arguments, ctx: ("shared-payroll", "1")
    entry = runtime.catalog.get("fake_pay")
    entry["policy"] = entry["policy"].model_copy(update={"timeout_s": 0.01})
    bank.delay = 0.15
    call = runtime.execute_batch((payment(),), context(runtime))[0]
    assert "unknown" in call.error
    other = runtime.execute_batch((payment(obligation_id="E0012"),), context(runtime))[0]
    assert "resource_in_flight" in other.error
    time.sleep(0.2)
    assert bank.calls == 1


def test_plan_contract_is_frozen_before_execution_and_missing_effect_cannot_finish(bank_runtime):
    _, _, runtime = bank_runtime
    ctx = context(runtime)
    plan = runtime.create_plan(Plan(scope_key=ctx.scope_key, actor_id="u", goal="支付", steps=(PlanStep(
        step_id="pay", goal="支付", target_ref="payroll:E0007", target_version="1", required_effects=("fake_pay",)),)))
    step = runtime.plans.claim(plan.plan_id)
    runtime.catalog.get("fake_pay")["policy"] = runtime.catalog.get("fake_pay")["policy"].model_copy(update={"implementation_version": "2"})
    policy = AssemblyPolicy(plan_id=plan.plan_id, step_id=step.step_id, step_lease_token=step.lease_token)
    planned = runtime.begin_turn(Session(session_id="s", author_id="u"), Message(), turn_id="planned", assembly_policy=policy)
    assert "contract_drift" in runtime.execute_batch((payment(),), planned)[0].error
    with pytest.raises(ValueError, match="plan_source_not_authorized"):
        runtime.create_plan(Plan(scope_key="u", actor_id="u", goal="修改", source_message_id="forged", steps=(PlanStep(step_id="x", goal="修改"),)))


def test_unknown_step_lease_cannot_be_reused(bank_runtime):
    _, _, runtime = bank_runtime
    ctx = context(runtime)
    plan = runtime.create_plan(Plan(scope_key=ctx.scope_key, actor_id="u", goal="支付", steps=(PlanStep(step_id="pay", goal="支付"),)))
    step = runtime.plans.claim(plan.plan_id)
    policy = AssemblyPolicy(plan_id=plan.plan_id, step_id=step.step_id, step_lease_token=step.lease_token)
    planned = runtime.begin_turn(Session(session_id="s", author_id="u"), Message(), turn_id="planned", assembly_policy=policy)
    runtime.plans.change_step(plan.plan_id, step.step_id, {"lease_token": "new-worker"})
    call = runtime.execute_batch((payment(),), planned)[0]
    assert "step_lease_lost" in call.error


def test_provider_ids_can_repeat_without_reusing_immutable_fact_ids(bank_runtime):
    _, _, runtime = bank_runtime
    request = payment()
    first = runtime.execute_batch((request,), context(runtime))[0]
    replay = runtime.execute_batch((request,), context(runtime))[0]
    assert first.tool_call_id != replay.tool_call_id
    assert build_tool_message(first).metadata["tool_call_id"] == request.id
    assert build_tool_message(replay).metadata["tool_call_id"] == request.id


def test_replayed_business_action_is_linked_to_new_plan_step(bank_runtime):
    bank, _, runtime = bank_runtime
    initial = runtime.execute_batch((payment(),), context(runtime))[0]
    ctx = context(runtime)
    plan = runtime.create_plan(Plan(scope_key=ctx.scope_key, actor_id="u", goal="确认付款", steps=(PlanStep(step_id="pay", goal="确认付款"),)))
    step = runtime.plans.claim(plan.plan_id)
    policy = AssemblyPolicy(plan_id=plan.plan_id, step_id=step.step_id, step_lease_token=step.lease_token)
    ctx = runtime.begin_turn(Session(session_id="s", author_id="u"), Message(), turn_id="plan-replay", assembly_policy=policy)
    replay = runtime.execute_batch((payment(),), ctx)[0]
    assert replay.ok and replay.metadata["action"]["ledger_entry_id"] == initial.metadata["action"]["ledger_entry_id"]
    effects = runtime.ledger.list(plan_id=plan.plan_id, step_id=step.step_id)
    assert len(effects) == 1 and effects[0].effect_state == "succeeded" and bank.calls == 1


def test_agent_binding_does_not_allow_a_broader_toolset(bank_runtime):
    _, storage, existing = bank_runtime
    config = ActionConfig(toolsets=(Toolset(tools=existing.catalog.names(), max_risk="high"), Toolset(name="empty")),
                          agent_toolsets={"reader": ("empty",)})
    runtime = ActionRuntime(storage.db, existing.catalog, config=config)
    try:
        with pytest.raises(ValueError, match="agent_toolset_not_allowed"):
            runtime.begin_turn(Session(), Message(), turn_id="forbidden", agent_id="reader")
        ctx = runtime.begin_turn(Session(), Message(), turn_id="allowed", agent_id="reader", assembly_policy=AssemblyPolicy(toolset="empty"))
        assert runtime.get_schemas(ctx) == []
    finally:
        runtime.stop()


def test_compensation_has_its_own_record_and_preserves_original_success(bank_runtime):
    bank, storage, existing = bank_runtime
    class Refund(Tool):
        name = "refund"
        description = "补偿原付款"
        parameters = {"type": "object", "properties": {"external_ref": {"type": "string"}}, "required": ["external_ref"]}
        calls = 0
        def execute(self, arguments):
            self.calls += 1
            return ToolCallResult(data={"external_ref": "refund:" + arguments["external_ref"]}, result_text="已退款")
    refund = Refund()
    existing.catalog.register(refund, ToolPolicy(when_to_use="补偿", when_not_to_use="正常发薪", effect="write", risk="medium",
        idempotency="keyed", key_fields=("external_ref",)), BuiltinToolAdapter(refund))
    pay = existing.catalog.get("fake_pay")
    pay["policy"] = pay["policy"].model_copy(update={"compensation_tool": "refund"})
    runtime = ActionRuntime(storage.db, existing.catalog, config=ActionConfig(toolsets=(Toolset(tools=existing.catalog.names(), max_risk="high"),)))
    try:
        ctx = context(runtime)
        call = runtime.execute_batch((payment(),), ctx)[0]
        item = runtime.recovery.compensate(call.metadata["action"]["ledger_entry_id"], ctx, {})
        assert item.effect_state == "succeeded" and item.status == "compensated"
        assert runtime.recovery.compensate(item.entry_id, ctx, {}) == item and refund.calls == 1
        replay = runtime.execute_batch((payment(),), context(runtime))[0]
        assert replay.metadata["action"]["status"] == "compensated" and bank.calls == 1
        assert len(runtime.ledger.list()) == 2
    finally:
        runtime.stop()
