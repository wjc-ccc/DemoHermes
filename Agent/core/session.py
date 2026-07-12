"""
Session — 会话生命周期管理

职责：
    - 创建新会话（分配 session_id，初始化上下文）
    - 恢复历史会话（从 episodic memory 加载）
    - 关闭会话（触发 evolution/reviewer 复盘）

一个 Session 对应一次完整的用户交互过程。
"""
