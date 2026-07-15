"""
API — HTTP / WebSocket 入口

供可视化前端接入的 Web 服务：
    - POST /chat          : 发送消息，获取 Agent 回复
    - WS  /events         : 实时事件流（tool_call、memory_write 等）
    - GET /sessions       : 列出历史会话
    - GET /sessions/{id}  : 获取会话详情与回放数据

实现"可视化所有步骤流程和完整上下文"的核心接口。
"""
