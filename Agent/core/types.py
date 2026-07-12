"""
Types — 核心数据结构

定义 Agent 各模块共用的数据类型：
    - Message      : 单条对话消息（role, content, tool_calls）
    - Turn         : 一轮完整对话（user 输入 → assistant 回复 → 工具结果）
    - ToolCall     : 工具调用请求（name, arguments, id）
    - MemoryItem   : 记忆条目（layer, content, metadata, timestamp）
    - SessionState : 会话状态（id, messages, hot_snapshot, created_at）

使用 dataclass 或 pydantic 实现，保证类型安全。
"""
