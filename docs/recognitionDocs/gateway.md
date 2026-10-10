# Gateway

Gateway 管入出站、Channel 与同会话队列。不同会话并行，同会话按序，排队不占 worker。

ask 用 correlation_id 等待出站，submit 只入队；/help、/new、/reset 在此处理。普通请求取 Session/MemoryScope，按需冻结 MemorySnapshot 后调用 Loop。

成功和失败均保存事实，失败消息不进入后续正常历史。Store 同事务提交事实、卡片、Action 回执和 outbox，成功后才通知提交/重开钩子。

Runtime 始终注入 SQLite 会话存储，Memory 和 Action 开关都不影响落库与重启恢复。Memory 关闭时仅省略记忆快照、记忆工具和后台提炼。

失败返回 persistence_error，涉及 memory_edit 暂存时返回 memory_write_failed。Runtime 管后台生命周期，Gateway 管调度和渠道。运行描述与 PrecisionTrace 由 Runtime 提供，开关见 [Runtime 架构](../runtime-architecture.md)。
