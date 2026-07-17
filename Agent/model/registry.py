"""
ModelRegistry — 模型注册表

按名称注册和获取 LLM Provider：
    register("deepseek", DeepSeekProvider)
    register("claude", ClaudeSDKProvider)
    get(MODEL)  # 从 Config.MODEL 读取默认模型

上层只需调用 registry.get()，无需关心具体 provider 实现。
"""
