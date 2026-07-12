"""
EpisodicDB — L2 情景记忆数据库

SQLite schema 定义与连接管理，主要表：
    - turns       : 每轮对话（session_id, role, content, timestamp）
    - tool_calls  : 工具调用记录（turn_id, tool_name, args, result）
    - sessions    : 会话元信息（id, created_at, summary）

数据库文件存放在 data/ 目录下。
"""
