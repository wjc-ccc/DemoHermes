# Types：跨模块数据契约

全局对话契约在 `agent/core/types.py`。Memory、Precision、Evolution、Eval、SQLite 各自还有局部 `types/` 或专用类型文件；协议与业务实现分开维护，不把所有类型堆进一个文件。

| 对象 | 流向与用途 | 关键字段或约束 |
| --- | --- | --- |
| `ContentPart` | Channel → Message → Model Adapter | `type`、`text`、附件的 `path/file_name/mime_type/size_bytes`；有图像和文件载体 |
| `SessionSource` | Channel → Gateway → Channel | `channel/chat_id/chat_type/user_id`；用于路由与作用域推导 |
| `InboundEvent` | Channel → Gateway | `text/content/source/channel_prompt/metadata`；`channel_prompt` 只在本轮使用 |
| `Message` | Gateway/Loop ↔ SessionStore | 消息 ID、序号、角色、内容、状态、`trajectory_id` |
| `ToolCall` | Loop ↔ SessionStore | 工具请求、执行状态、耗时、结果与错误 |
| `Session` | Gateway → Loop → Store | `messages`、`tool_calls`、`session_key`、`context_anchor`；`add_message` 维护序号和时间 |
| `Anchor` / `AnchorFact` | Precision ↔ Session | 带版本、覆盖区间和来源的历史压缩结果 |
| `TurnResult` | Loop → Gateway | `final_text/session_id/turn_id/completed/interrupted/error` |
| `OutboundReply` | Gateway → Channel | 文本、来源、回复目标和 metadata |

## 各模块内部契约

- `agent/core/memory/types/` 定义提案、来源、Store 与后台工作协议；`memory_scope.py`、`memory_snapshot.py`、`memory_item.py` 定义作用域、快照和原子记忆项。
- `agent/core/precision/percision_types.py` 定义 `ContextItem`、`PrecisionResult`、指标和记录。当前文件路径确实拼作 `percision`。
- `agent/evolution/types.py` 与 `agent/eval/types.py` 分别承载 Skill 提案/回放以及评测 case/报告。
- `agent/model/baseStructure.py` 定义 `ModelResponse` 与工具请求；`agent/tools/base.py` 定义工具调用结果。

新增字段时先确认谁是生产者、谁是消费者，再更新其所属 Interface。传输对象不负责读取数据库或调用模型；`Session.add_message` 这类维护自身不变量的小方法是例外。`ContentPart` 能表达某种媒体，并不代表渠道、模型适配器和实际模型都已支持该媒体的语义处理。
