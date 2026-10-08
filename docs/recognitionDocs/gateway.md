# Gateway：路由、会话与提交

代码入口：`agent/gateway/gateway.py:Gateway`；队列见 `message_bus.py`，会话键见 `session_key.py`，HTTP 入口见 `http_api.py`，SQLite 实现见 `agent/storage/sqlite/session_store.py`。

## Interface

| 方法 | 行为 |
| --- | --- |
| `register_channel(channel)` | 按名称注册并回绑 Channel |
| `submit(event)` / `submit_raw(name, raw)` | 异步入站；raw 先交给 Channel 解析 |
| `ask(event, timeout)` | 仍走队列，通过 `correlation_id` 等待出站回复 |
| `start()` / `stop()` | 管理入站、出站线程及注入的后台工作生命周期 |
| `status()` | 提供运行状态快照 |

## 一条消息的处理顺序

1. `InboundEvent` 进入 `MessageBus.in_dto`；入站分发线程把它交给线程池。
2. `_process_inbound` 根据 `SessionSource` 计算 `session_key`，用每键一把锁使同会话在本进程内串行；不同键可在线程池中并行。
3. `/new`、`/reset`、`/help` 在 Gateway 短路。普通请求由 `build_message` 转成用户 `Message`，再查找或创建 Session。
4. 启用记忆时，Gateway 推导 `MemoryScope`，调用 `MemoryContextLoader.freeze`，将固定快照交给 `AgentLoop.run_turn`。关闭记忆时直接走 `DictSessionStore` 路径。
5. 持久化路径将 `TurnRecord`、Session 中的消息和工具调用提交到 SQLite。成功后才通知 `on_turn_committed`，由 EvolutionScheduler 扇出 Reviewer、Skill Evolution、环境快照等工作。
6. `OutboundReply` 进入出站队列；出站线程先唤醒同步 `ask` 等待者，再根据 `reply.source.channel` 找对应 Channel 投递，并发布 `outbound_reply` 事件。

## 失败与持久化

`build_gateway(use_memory=True)` 通过 `RuntimeStorage.open` 注入 `SqliteSessionStore`；`--no-use-memory` 使用内存中的 `DictSessionStore`。普通持久化请求使用 `commit_turn`，明确的 `memory_edit` 卡片变更与本轮事实同事务提交。提交失败时回复 metadata 带 `persistence_error`；涉及记忆写入时还会返回 `memory_write_failed`，避免误称已经记住。未处理异常会尽力记录失败轮次。

Gateway 不组装模型提示词，也不解析平台 SDK 的载荷。它只把对象交给对应模块：平台解析在 Channel，模型迭代在 Loop，上下文取舍在 Precision，实际数据库写入在 Store。这里的每键锁只约束当前 Gateway 进程，不提供跨进程分布式锁。

HTTP API、SSE 与 Debug 查询位于 `agent/gateway/http_api.py`、`trace_modules.py`；它们调用 Gateway 或只读 Store 查询，供 `frontier/` 展示对话和轨迹。
