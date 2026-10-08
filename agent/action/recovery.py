"""只依据账本和外部回执恢复；查询不可见不等于确认未执行。"""
import uuid
from ..core.memory.memory_scope import MemoryScope
from ..model.baseStructure import ToolCallRequest
from ..tools.base import ToolCallResult
from .context import ToolContext
from .ledger import now_ms


class ActionRecovery:
    def __init__(self, runtime):
        self.runtime = runtime

    def recover(self, *, scope_key=None):
        runtime = self.runtime
        recovered = []
        for item in runtime.ledger.list(scope_key=scope_key, unfinished=True):
            if runtime.executor.is_active(item.entry_id):
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
                "contract_hashes": {name: runtime.catalog.contract_hash(name) for name in context.toolset.tools
                                    if runtime.catalog.get(name)}})
            properties = runtime.catalog.contract(policy.recovery_tool)["parameters"].get("properties", {})
            args = {name: value for name, value in item.effective_args.items() if name in properties}
            if "idempotency_key" in properties:
                args["idempotency_key"] = item.idempotency_key
            if "external_ref" in properties and item.external_ref:
                args["external_ref"] = item.external_ref
            request = ToolCallRequest(id=uuid.uuid4().hex, name=policy.recovery_tool, arguments=args)
            call = runtime.execute_batch((request,), context)[0]
            data = call.tool_call_result or {}
            current = runtime.ledger.get(item.entry_id)
            if current.effect_state == "succeeded":
                recovered.append(current)
                continue
            if call.ok and data.get("status") == "succeeded":
                raw = ToolCallResult(ok=True, result_text="外部系统确认成功", data=data.get("result") or data)
                _, current = runtime.executor.settle(current, raw, context, item.effective_args, policy)
            elif call.ok and data.get("status") == "failed" and data.get("confirmed_no_effect") is True:
                current = runtime.ledger.update(current, changes={"effect_state": "failed", "phase": "finished",
                    "lease_expires_at": None}, event_type="confirmed_no_effect", evidence=data)
                runtime.ledger.release(current)
            recovered.append(current)
        return tuple(recovered)

    def compensate(self, entry_id, context, arguments):
        item = self.runtime.ledger.get(entry_id)
        if not item or item.scope_key != context.scope_key or item.effect_state != "succeeded":
            raise ValueError("补偿需要同 scope 已确认成功的原动作")
        policy = self.runtime.catalog.policy_for(item.tool_name, item.effective_args)
        if not policy.compensation_tool:
            raise ValueError("工具未声明 compensation_tool")
        item = self.runtime.ledger.update(item, changes={"compensation_state": "started"}, event_type="compensation_requested")
        call = self.runtime.execute_batch((ToolCallRequest(id=uuid.uuid4().hex,
            name=policy.compensation_tool, arguments=arguments),), context)[0]
        compensation = self.runtime.ledger.get(call.metadata["action"]["ledger_entry_id"])
        state = "succeeded" if compensation.effect_state == "succeeded" else (
            "failed" if compensation.effect_state == "failed" else "unknown")
        return self.runtime.ledger.update(item, changes={"compensation_state": state}, event_type="compensation_result",
            evidence={"compensation_entry_id": compensation.entry_id})

