"""只依据账本和外部回执恢复；查询不可见不等于确认未执行。"""
import uuid
from agent.types.memory.scope import MemoryScope
from agent.types.model import ToolCallRequest
from agent.types.tools import ToolCallResult, ToolTurnContext
from agent.types.action.execution import ToolContext, now_ms


class ActionRecovery:
    def __init__(self, runtime):
        self.runtime = runtime

    def recover(self, *, scope_key=None):
        runtime = self.runtime
        recovered = []
        for item in runtime.ledger.list(scope_key=scope_key, unfinished=True):
            if runtime.dispatch.is_active(item.entry_id):
                recovered.append(item)
                continue
            if item.lease_expires_at and item.lease_expires_at > now_ms():
                recovered.append(item)
                continue
            if item.phase == "prepared":
                # 提交 hook 与卡片写入原子提交；过期暂存没有成功回执就不重发。
                # 原领取凭证取消后，迟到的 commit_turn 会在 before_commit 被拒绝。
                with runtime.ledger.db.write() as conn:
                    old = item
                    item = runtime.ledger.update(item, changes={"effect_state": "failed", "phase": "finished",
                        "lease_token": None, "lease_expires_at": None}, event_type="orphaned_preparation_cancelled", conn=conn)
                    runtime.ledger.release(old, conn=conn)
                recovered.append(item)
                continue
            if item.effect == "read":
                item = runtime.ledger.update(item, changes={"effect_state": "failed", "phase": "finished"},
                                             event_type="orphaned_read")
                recovered.append(item)
                continue
            if item.effect_state == "started":
                item = runtime.ledger.update(item, changes={"effect_state": "unknown"}, event_type="recovery_required")
            entry = runtime.catalog.get(item.tool_name)
            policy = runtime.catalog.policy_for(item.tool_name, item.effective_args) if entry else None
            if not policy or not policy.recovery_tool or item.recovery_count >= policy.max_recovery_queries:
                recovered.append(item)
                continue
            query_entry = runtime.catalog.get(policy.recovery_tool)
            if query_entry is None:
                recovered.append(item)
                continue
            item = runtime.ledger.update(item, changes={"recovery_count": item.recovery_count + 1}, event_type="recovery_query")
            context = ToolContext.model_validate(item.context)
            if context.memory_identity:
                context = context.model_copy(update={"memory_scope": MemoryScope.model_validate(context.memory_identity)})
            context = context.model_copy(update={"turn_id": "recovery:" + uuid.uuid4().hex,
                "plan_id": None, "step_id": None, "step_lease_token": None, "target_ref": None, "target_version": None,
                "contract_hashes": {name: runtime.catalog.contract_hash(name) for name in context.toolset.tools
                                    if runtime.catalog.get(name)}})
            properties = runtime.catalog.contract(policy.recovery_tool)["parameters"].get("properties", {})
            args = {name: value for name, value in item.effective_args.items() if name in properties}
            if "idempotency_key" in properties:
                args["idempotency_key"] = item.idempotency_key
            if "external_ref" in properties and item.external_ref:
                args["external_ref"] = item.external_ref
            request = ToolCallRequest(id=uuid.uuid4().hex, name=policy.recovery_tool, arguments=args)
            call = runtime.handler.handle_batch((request,), ToolTurnContext.from_action(context))[0]
            data = call.tool_call_result or {}
            current = runtime.ledger.get(item.entry_id)
            if current.effect_state == "succeeded":
                recovered.append(current)
                continue
            if call.ok and data.get("status") == "succeeded":
                raw = ToolCallResult(ok=True, result_text="外部系统确认成功", data=data.get("result") or data)
                _, current = runtime.dispatch.settle(current, raw, context, item.effective_args, policy)
            elif call.ok and data.get("status") == "failed" and data.get("confirmed_no_effect") is True:
                current = runtime.ledger.update(current, changes={"effect_state": "failed", "phase": "finished",
                    "lease_expires_at": None}, event_type="confirmed_no_effect", evidence=data)
                runtime.ledger.release(current)
            recovered.append(current)
        for source in runtime.ledger.list(scope_key=scope_key):
            if source.compensation_state not in {"started", "unknown"}:
                continue
            compensations = [item for item in runtime.ledger.list(scope_key=source.scope_key)
                             if item.context.get("compensates_entry_id") == source.entry_id]
            if compensations:
                latest = compensations[-1]
                state = latest.effect_state if latest.effect_state in {"succeeded", "failed"} else "unknown"
                runtime.ledger.update(source, changes={"compensation_state": state}, event_type="compensation_reconciled",
                                      evidence={"compensation_entry_id": latest.entry_id})
        return tuple(recovered)

    def compensate(self, entry_id, context, arguments):
        item = self.runtime.ledger.get(entry_id)
        if not item or item.scope_key != context.scope_key or item.effect_state != "succeeded":
            raise ValueError("补偿需要同 scope 已确认成功的原动作")
        if item.compensation_state == "succeeded":
            return item
        if item.compensation_state in {"started", "unknown"}:
            raise ValueError("补偿尚未对账")
        policy = self.runtime.catalog.policy_for(item.tool_name, item.effective_args)
        if not policy.compensation_tool:
            raise ValueError("工具未声明 compensation_tool")
        properties = self.runtime.catalog.contract(policy.compensation_tool)["parameters"].get("properties", {})
        reference_name = "external_ref" if "external_ref" in properties else ("idempotency_key" if "idempotency_key" in properties else None)
        if reference_name is None:
            raise ValueError("补偿契约必须明确引用原动作的 external_ref/idempotency_key")
        reference = item.external_ref if reference_name == "external_ref" else item.idempotency_key
        if not reference or arguments.get(reference_name, reference) != reference:
            raise ValueError("补偿引用不匹配")
        arguments = {**arguments, reference_name: reference}
        item = self.runtime.ledger.update(item, changes={"compensation_state": "started"}, event_type="compensation_requested")
        call = self.runtime.handler.handle_batch((ToolCallRequest(id=uuid.uuid4().hex,
            name=policy.compensation_tool, arguments=arguments),),
            ToolTurnContext.from_action(context.model_copy(update={"compensates_entry_id": entry_id})))[0]
        compensation = self.runtime.ledger.get(call.action_evidence.ledger_entry_id)
        state = "succeeded" if compensation.effect_state == "succeeded" else (
            "failed" if compensation.effect_state == "failed" else "unknown")
        return self.runtime.ledger.update(item, changes={"compensation_state": state}, event_type="compensation_result",
            evidence={"compensation_entry_id": compensation.entry_id})

