# AgentLoop：单轮推理与工具循环

代码入口：`agent/core/agent_loop.py:AgentLoop.run_turn`。相关实现为 `agent/core/precision/`、`agent/core/building_messages.py`、`agent/model/`、`agent/core/event_bus.py`。当前代码中的 Precision 文件名拼写为 `percision_*.py`，查找源码时需使用这个实际路径。

## Interface 与职责

`run_turn(session, user_message, ..., memory_snapshot, memory_scope, context_budget, skill_query, skill_selector) -> TurnResult`。Gateway 负责提供 Session、冻结的记忆快照和作用域；Loop 不解析平台协议，也不提交数据库事务。

1. 生成 `turn_id`，把用户 `Message` 加入 Session，发布 `turn_start`。
2. 每个 turn 冻结一次 `SkillSelection`。模型调用迭代期间复用这份选择和同一份 `MemorySnapshot`。
3. 每轮迭代调用 `PrecisionBuilder.build`，固定经过 load → grade → build。输入包括系统提示、Skill 摘要、记忆、历史、临时通道说明；输出是模型消息、指标与逐项取舍记录。
4. `model.chat` 返回工具请求时，Loop 用 `ToolRegistry.dispatch` 执行，记录 `ToolCall`，把工具结果消息回注 Session，再进行下一次模型调用。
5. 模型返回纯文本时，追加 assistant 消息，发布 `turn_end` 并返回完成的 `TurnResult`；打断、预算超限、Anchor 压缩失败、模型失败或达到迭代上限则返回未完成结果。

```text
Session + 用户 Message + MemorySnapshot
  → freeze SkillSelection
  → PrecisionBuilder.build → messages
  → model.chat
      ├─ tool_calls → dispatch → ToolCall/role=tool Message → 再次 build
      └─ text → assistant Message → TurnResult
```

## Precision 是上下文治理点

`PrecisionBuilder.build` 把来源转换为 `ContextItem`，按 P0–P3 决定保留、压缩、延迟暴露或丢弃。历史过长时用有覆盖账本的 `Anchor`；`ContextBudget` 限制整个模型输入。结果是 `PrecisionResult(messages, metrics, records, exposed_refs, anchor)`，并写入 `PrecisionTrace`。P3 项的原文可按引用通过 `memory_read` 取得。

Loop 的模型调用前，`agent/model/media_messages.py` 根据适配器的 `supports_images` 处理图片输入；这不表示任意模型都能理解图像。模型调用可有限重试，工具失败会作为结果回注模型。`_chat_with_schema_wrong` 目前仍是预留方法，不能写成已启用的结构化输出恢复机制。

## 模块接口

| 连接 | 数据或调用 | 所有权 |
| --- | --- | --- |
| Gateway → Loop | `Session`、用户 `Message`、`MemorySnapshot`、作用域和预算 | Gateway 查会话、冻结记忆 |
| Precision → Loop | `PrecisionResult.messages`、Anchor 和指标 | Precision 决定上下文取舍 |
| Model → Loop | 文本、工具请求、用量和结束原因 | Model Adapter 负责供应商协议 |
| Loop → Tool | 工具名和参数；记忆工具由 Loop 注入 `_scope` | Tool 执行具体动作 |
| Loop → Gateway | `TurnResult` 与更新后的 Session | Gateway 持久化、包装回复 |

定位问题时先看 `turn_id`：`EventBus` 的 `llm_request/llm_response/tool_call_*`、`PrecisionTrace` 和 SQLite 的 Turn/ToolCall 可以串起一轮轨迹。
