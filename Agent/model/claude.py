"""
ClaudeSDKProvider — Claude Agent SDK 接入

通过 claude-agent-sdk 调用 Claude 模型，复现 Claude 原生 Agent 能力：
    - 工具调用（tool use）
    - 流式输出（streaming）
    - 多轮对话上下文管理

配置从 Agent.Config 读取 ANTHROPIC_API_KEY 和 ANTHROPIC_BASE_URL。
继承 BaseModel，实现 chat() 方法。
"""
