"""
ToolRegistry — 工具注册表

管理所有可用工具的注册、schema 收集与调用分发：
    register(tool)           : 注册工具实例
    get_schemas()            : 返回所有工具的 JSON Schema（注入 prompt）
    dispatch(name, args)     : 按名称分发执行，返回结果
    check_availability(name) : 检查工具是否可用（权限、环境）

core/loop 在 think 阶段将 schemas 传给 LLM，在 act 阶段通过 dispatch 执行。
"""
