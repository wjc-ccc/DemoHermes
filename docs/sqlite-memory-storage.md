# SQLite 与 Memory 存储（2026-10-11）

`RuntimeStorage.open` 是生产数据库装配入口。迁移脚本按版本和 SHA256 校验执行；`005_memory_cleanup.sql` 在一个事务中下线旧手册同步状态与 Task 三态表。已应用的 `001`–`004` 文件不改写。现有 `data/agent.db` 升级前的 SQLite backup 位于 `data/agent.pre_memory_cleanup_20260928.db`。

| 表 | 作用 |
| --- | --- |
| `sessions`、`turns`、`messages`、`tool_calls` | 会话、轮次和原始消息/工具事实；失败或中断原文仍保存 |
| `memory_review_turns`、`jobs` | 用户 scope 累计调度、固定批次来源、租约、有限重试 |
| `memory_cards`、`memory_experiences` | 四类原子用户卡及经验结构 |
| `memory_evidence`、`memory_terms` | 逐字证据引用与关键词索引 |
| `memory_card_versions`、`memory_conflicts` | 历史版本、无法自动解决的同槽位冲突 |
| `memory_document_versions`、`context_documents` | 个人手册版本及已登记 Markdown 路径/指纹 |
| `traces`、`trace_items`、`audit_events` | 提取/校验/写入观测与审计 |
| `eval_datasets`、`eval_cases`、`eval_questions` | 冻结的标准、内容版本、问题字段版本与历史检查点 |
| `eval_runs`、`eval_results` | 真实运行、问题指纹、实际回答、评分与复核历史 |

`memory.extract` 是唯一新卡提炼作业。提取器输出 `cards` 和 `skipped`：空 `cards` 完成作业但不新增卡；无效 JSON 或模型调用异常重试，单张提案不合格只拒该张。建卡必须说明保存理由、未来用途并通过原文证据校验；跳过原因也写入 Trace。助手上下文可用于解释跳过，但不能成为卡片证据。卡片、Trace 和 Job 完成同事务提交。更正和删除在正常完成轮次的 SessionStore 事务内处理，要求已有卡的 ID 与预期修订号。旧证据不能覆盖较新的明确更正或删除。

`010_storage_contract.sql` 保留此前迁移校验和，统一核心完整对象列为 `session_json`、`turn_json`、`message_json`、`tool_call_json`。轮次时间和错误使用 `finished_at`、`error_text`。会话数量由消息派生，文档登记删除没有消费者的 `revision_ref` 和 `tokenizer_name`。工具结构化结果与可读文本分别保存在 `result_data` 和 `result_text`；模型协议 ID 与持久工具事实 ID 分开。

每轮 `turn_json.perception_trace.builds` 保存全部构建、实际请求消息和工具 schema、主模型尝试与 Anchor 调用。主模型实际 usage 聚合写入轮次 token 列，缺失用量为 NULL。完整原文始终保存，投影、Anchor 和分页只影响模型输入。关闭 Memory 也继续执行显式上下文预算。

`MEMORY.md` 在 `data/memory/<scope_key>/MEMORY.md`，`memory_document_versions` 保存人工与模型修改历史。手册不产生卡片提炼作业，新卡也不写手册。原先的 `memory_document_sync` 状态表、`memory.manual_sync`/`memory.manual_maintain` 作业和 Task 工作台/叙事/精确状态表已下线；未完成的旧同步作业升级时取消。仅有手册证据、没有对话或工具原文证据的旧卡隔离且保留版本历史。

调试 API 仍保留原始会话、轮次、Memory Trace、数据库表和行的只读投影；`/api/debug/memory/states` 已移除。前端“会话三态”页面及路由也已移除。

