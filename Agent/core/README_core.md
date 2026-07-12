# core — Agent 核心引擎

Agent 的"大脑中枢"，负责对话循环、会话管理和上下文组装。

## 文件说明

| 文件 | 职责 |
|------|------|
| `loop.py` | **主循环**：observe → think → act → persist，每轮对话的调度入口 |
| `session.py` | 会话生命周期：创建、恢复、关闭，维护对话状态 |
| `context_builder.py` | 组装发给 LLM 的上下文（Hot 快照 + 记忆检索 + Skills + 当前消息） |
| `events.py` | 统一事件模型（turn_start、tool_call 等），供可视化与回放 |
| `types.py` | 核心数据结构：Message、Turn、ToolCall、MemoryItem 等 |
| `__init__.py` | 包初始化 |

## 与其他模块的关系

- 调用 `model/` 进行 LLM 推理
- 调用 `tools/` 执行工具
- 通过 `memory/manager.py` 读写记忆
- 通过 `prompt/` 构建 System Prompt
- 向 `gateway/api.py` 推送 `events` 供前端展示
