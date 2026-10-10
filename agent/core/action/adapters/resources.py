"""只在可信 scope 中定位资源；读记录必须绑定实际返回的版本。"""


def card_in_scope(cards, context, identity):
    item = cards.get(str(identity).removeprefix("card:")) if cards else None
    return item if item and item.scope.storage_key == context.scope_key else None


def resource_for(tool_name, arguments, context, *, cards=None, manual=None, skills=None):
    if tool_name == "memory_edit":
        if arguments.get("operation", "").startswith("card_"):
            identity = str(arguments.get("item_id", "")).removeprefix("card:")
            item = card_in_scope(cards, context, identity)
            return "card:" + identity, str(item.revision) if item else None
        if manual and context.memory_scope:
            current = manual.read(context.memory_scope)
            return "manual:" + context.scope_key, current.disk_hash
    if tool_name == "memory_read" and str(arguments.get("reference", "")).startswith("card:"):
        ref = arguments["reference"]
        item = card_in_scope(cards, context, ref)
        return ref, str(item.revision) if item else None
    if tool_name in {"skill_manage", "use_skill"}:
        name = arguments.get("name") or arguments.get("skill_name") or ""
        if tool_name == "use_skill" and context.skills:
            ref = context.skills.load(name)
            return "skill:" + name, str(ref.revision) if ref else None
        current = skills.get(name) if skills else None
        return "skill:" + name, str(current.revision) if current else "0"
    return None, None

