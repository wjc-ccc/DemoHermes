# 架构认知文档

本目录只描述当前代码已经接线的行为。先读 [七个能力模块总览](../模块架构总览.md)，再按问题进入以下文档。历史设计、已替换的流程和阶段计划保留在 `docs/aiDocs/`。

| 想了解的问题 | 当前文档 | 主要代码 |
| --- | --- | --- |
| 跨模块传递什么数据 | [types](types.md) | `agent/core/types.py` 及各模块的 `types/` |
| 消息和附件怎样进入系统 | [channel](channel.md) | `agent/channel/`、`agent/model/media_messages.py` |
| 谁控制会话、并发和持久化提交 | [gateway](gateway.md) | `agent/gateway/`、`agent/storage/sqlite/` |
| 模型、上下文和工具如何循环 | [agent loop](agent%20loop.md) | `agent/core/agent_loop.py`、`agent/core/precision/` |
| Tool、Skill、Plugin 的实际状态 | [tool / skill / plugin](tool&skill&plugin.md) | `agent/tools/`、`agent/skills/`、`agent/evolution/` |
| 长期信息怎样读取、写入和复核 | [memory](memory.md) | `agent/core/memory/`、`agent/storage/sqlite/` |

## 主链

```text
Channel.parse_to_InboundEvent
  → Gateway.submit / ask → MessageBus.in_dto
  → 按 session_key 加锁 → MemoryContextLoader.freeze
  → AgentLoop.run_turn → PrecisionBuilder.build → model.chat
  → 若有 tool_calls：ToolRegistry.dispatch → ToolCall 回注 → 再次 model.chat
  → SqliteSessionStore.commit_turn → EvolutionScheduler.notify
  → MessageBus.out_dto → Channel.deliver_to_OutboundEvent
```

`agent/main.py:build_gateway` 是核对生产装配的起点。默认启用 Memory 时使用 SQLite；`--no-use-memory` 使用 `DictSessionStore`，没有持久化记忆旁路。HTTP Debug、Trace 和前端视图是观测入口，不负责对话决策。

## 职责定位

| 变更内容 | 放在何处 |
| --- | --- |
| 平台协议、下载、发送、去重 | Channel |
| 会话键、并发锁、斜杠命令、提交后通知 | Gateway |
| 上下文选择、Anchor、预算 | Precision |
| 模型迭代、工具回注、错误结果 | AgentLoop |
| 原子工具行为、Skill 文本和状态 | Tool / Skill |
| 记忆快照、检索、卡片及手册版本 | Memory + Store |
| 已提交轨迹的 Skill 提案与回放 | Evolution + Eval |

模块之间通过明确的 Interface 连接。增加功能时先定位它消费和产出的数据，再在负责该行为的模块中实现；必要时更新本目录与总览。
