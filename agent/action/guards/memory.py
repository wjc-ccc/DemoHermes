"""记忆工具 PRE；业务写入口仍保留对应规则。"""
from ..adapters.resources import card_in_scope


def check_memory(arguments, context, adapter):
    operation = arguments.get("operation")
    if not context.memory_scope or not context.actor_id:
        return "missing_memory_scope"
    if operation in {"card_update", "card_delete"}:
        item = card_in_scope(adapter.cards, context, arguments.get("item_id", ""))
        if item is None:
            return "resource_not_in_scope"
        if item.status.value != "active":
            return "inactive_card"
        if "expected_revision" not in arguments:
            return "stale_precondition"
        if operation == "card_update":
            if not arguments.get("summary", "").strip() or not arguments.get("content", "").strip():
                return "missing_card_content"
            fields = ("applicability", "action", "outcome", "limitations", "outcome_status")
            if item.kind.value != "reusable_experience" and any(name in arguments for name in fields):
                return "fact_fields_not_allowed"
    elif operation in {"manual_replace", "manual_restore"}:
        if not arguments.get("expected_hash") or not arguments.get("reason", "").strip():
            return "stale_precondition"
        if operation == "manual_restore" and "revision" not in arguments:
            return "missing_restore_revision"
        if operation == "manual_replace" and "content" not in arguments:
            return "missing_manual_content"
    return None

