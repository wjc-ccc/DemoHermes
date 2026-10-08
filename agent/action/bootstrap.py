"""可信内置工具定级与适配；未知工具必须显式注册，不能自动授予权限。"""
from .catalog import ActionToolCatalog
from .policy import ToolPolicy
from .runtime import ActionRuntime
from .adapters.builtin import BuiltinToolAdapter


def build_action_runtime(storage, registry, *, config=None, event_bus=None, skill_repository=None):
    catalog = ActionToolCatalog()
    for name in registry.list_tools():
        tool = registry.get(name)
        adapter = BuiltinToolAdapter(tool, cards=storage.memory,
            manual=getattr(tool, "_manual", None), skills=skill_repository)
        read = ToolPolicy(when_to_use=tool.description or "按任务读取或计算", when_not_to_use="不用于产生业务写入")
        if name in {"calculator", "use_skill", "memory_search", "memory_read"}:
            catalog.register(tool, read, adapter)
        elif name == "skill_manage":
            policy = ToolPolicy(when_to_use="创建或修正技能候选", when_not_to_use="不用于修改内置技能或自动晋升",
                effect="write", risk="medium", permission="user", idempotency="keyed",
                key_fields=("action", "name", "expected_revision"))
            catalog.register(tool, policy, adapter, operation_policies={
                "patch": policy.model_copy(update={"precondition_field": "expected_revision"})})
        elif name == "memory_edit":
            policy = ToolPolicy(when_to_use="用户明确更正已有记忆或个人手册", when_not_to_use="不用于创建事实卡或改变类别",
                effect="write", risk="medium", permission="user", idempotency="keyed",
                key_fields=("operation", "item_id", "expected_revision", "expected_hash", "revision"))
            card = policy.model_copy(update={"precondition_field": "expected_revision", "deferred_commit": True})
            manual = policy.model_copy(update={"precondition_field": "expected_hash"})
            catalog.register(tool, policy, adapter, operation_policies={
                "manual_get": read, "card_update": card, "card_delete": card,
                "manual_replace": manual, "manual_restore": manual})
        else:
            raise ValueError(f"工具缺少 action 定级: {name}")
    runtime = ActionRuntime(storage.db, catalog, config=config, event_bus=event_bus)
    storage.sessions.attach_action_commit_hook(runtime.commit_hook)
    return runtime

