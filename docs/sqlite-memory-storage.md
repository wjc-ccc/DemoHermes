# SQLite 与 Memory 存储（2026-09-28）

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

`memory.extract` 是唯一新卡提炼作业。`[]` 完成作业但不新增卡；无效 JSON 或模型调用异常重试，单张提案不合格只拒该张。卡片、Trace 和 Job 完成同事务提交。更正和删除在正常完成轮次的 SessionStore 事务内处理，要求已有卡的 ID 与预期修订号。旧证据不能覆盖较新的明确更正或删除。

`MEMORY.md` 在 `data/memory/<scope_key>/MEMORY.md`，`memory_document_versions` 保存人工与模型修改历史。手册不产生卡片提炼作业，新卡也不写手册。原先的 `memory_document_sync` 状态表、`memory.manual_sync`/`memory.manual_maintain` 作业和 Task 工作台/叙事/精确状态表已下线；未完成的旧同步作业升级时取消。仅有手册证据、没有对话或工具原文证据的旧卡隔离且保留版本历史。

调试 API 仍保留原始会话、轮次、Memory Trace、数据库表和行的只读投影；`/api/debug/memory/states` 已移除。前端“会话三态”页面及路由也已移除。

