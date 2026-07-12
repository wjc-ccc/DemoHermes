"""
EpisodicRecorder — L2 对话轨迹记录器

在每轮对话结束时，将完整轨迹写入 SQLite：
    - 用户消息与 Agent 回复
    - 所有工具调用及其参数、返回结果
    - 时间戳与 session 关联

为可视化回放和 evolution/reviewer 复盘提供数据基础。
"""
