"""可信内置工具定级与适配；未知工具必须显式注册，不能自动授予权限。"""
from .catalog import ActionToolCatalog
from .runtime import ActionRuntime
from .adapters.builtin import BuiltinToolAdapter


def build_action_runtime(storage, registry, *, config=None, event_bus=None, skill_repository=None, trace=None):
    """把注册表里的工具装配成受控执行运行时。

    定级来自工具自身的 action_policy / action_operations 声明；装配处只做校验，
    不再按工具名硬编码。未声明定级的工具视为未授权，装配期直接失败，
    避免运行到执行阶段才静默放行。
    """
    catalog = ActionToolCatalog()
    for name in registry.list_tools():
        tool = registry.get(name)
        policy = getattr(tool, "action_policy", None)
        if policy is None:
            raise ValueError(f"工具缺少 action 定级: {name}")
        adapter = BuiltinToolAdapter(tool, cards=storage.memory,
            manual=getattr(tool, "_manual", None), skills=skill_repository)
        operations = getattr(tool, "action_operations", None) or {}
        catalog.register(tool, policy, adapter, operation_policies=operations)
    runtime = ActionRuntime(storage.db, catalog, config=config, event_bus=event_bus, trace=trace)
    storage.sessions.attach_action_commit_hook(runtime.commit_hook)
    return runtime

