"""
Events — 统一事件模型

定义 Agent 运行中产生的所有事件类型，供两个用途：
    1. 可视化前端实时展示（gateway/api 推送 WebSocket）
    2. 对话回放与复现（按事件序列重建完整上下文）

事件类型示例：turn_start, llm_response, tool_call, tool_result,
              memory_write, evolution_trigger, session_end
"""
