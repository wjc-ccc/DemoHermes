# 当前架构认知

先读 [AgentRuntime 运行架构](../runtime-architecture.md)。

| 问题 | 文档 | 实现 |
| --- | --- | --- |
| 数据 | [types](types.md) | agent/types/ |
| 消息与附件 | [channel](channel.md) | channel/、model/media_messages.py |
| 会话与提交 | [gateway](gateway.md) | gateway/、storage/sqlite/ |
| 模型迭代 | [agent loop](agent%20loop.md) | core/agent_loop.py、perception/ |
| 工具与技能 | [tool / skill / plugin](tool&skill&plugin.md) | Handler、Dispatch、tools/、skills/ |
| 记忆 | [memory](memory.md) | MemoryBuilder、core/memory/、storage/sqlite/ |

主链：Channel → Gateway → Memory 快照 → Loop → Perception/Model/Handler/Dispatch → Store → Reviewer 通知 → 回复。

当前无 Skill Evolution；历史方案不能作为生产能力的证据。
