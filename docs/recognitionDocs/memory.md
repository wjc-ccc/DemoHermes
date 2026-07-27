# memory — 记忆系统认知

> 设计契约与 ADR 的唯一权威：`docs/aiDocs/7.23 Memory架构TDD方案.md`（含 §17 验收记录与已知缺口）。本文只讲"它是什么、边界在哪、如何相连"，方便判断需求归属；契约细节以方案文档为准。

## 一句话定位

memory 是独立深模块：**SQLite transcript 是事实源，Hot（USER/GROUP/MEMORY.md）与 Daily 是从事实提炼的可读视图，外部 Provider 是可插拔的黑盒补充**。ContextBuilder 只消费构建好的记忆文本，不知道文件、SQLite 或 Provider 的存在。

## 四种能力

| 能力 | 形态 | 定位 |
|------|------|------|
| 精选记忆（Hot） | `USER.md` / `GROUP.md` + `MEMORY.md` | 有界偏好与长期约定；整文件注入，受 1375/2200 字符硬上限 |
| Daily | `daily/YYYY-MM-DD.md` | 人类可读的每日事件归纳；默认不进 prompt，按需检索 |
| SQLite transcript | sessions / messages / tool_calls / review_jobs | 永久审计事实；完成记录不可变，异 payload 重提报 audit_conflict |
| 外部 Provider | 同时最多一个 | recall 失败/超时降级不阻断主对话；显式 CRUD 失败不静默 |

## 边界（绝不做的事）

- **MemoryManager** 是精选记忆唯一入口：只暴露 `build_context / read / apply / search`，无裸 `write(path, text)`；工具与 Reviewer 都走它。
- **SessionStore** 管会话生命周期：`commit_turn`（事实 + review outbox 同事务）、`close_and_reset`（单事务切换，旧 transcript 永留）。
- **ContextBuilder** 只读 `MemoryContextSnapshot.prompt`（frozen、带围栏）；同一 turn 所有模型迭代复用同一快照，下一 turn 才见更新。
- 工具（`memory_read` / `memory_edit`）是 Manager 薄 adapter：不暴露绝对路径/SQL，scope 由运行上下文注入，不能改 transcript。
- 单进程保证：进程级锁注册表 + 原子写；不声称支持多进程 writer（方案 §5.2）。

## 如何相连（一条消息的记忆路径）

```
InboundEvent → build_memory_scope(source)           # personal/group + channel 隔离，sha256 目录
  → Manager.build_context(scope, query, session_id) # 不可变快照：profile + MEMORY + Provider recall（+ 可选今日 daily）
  → AgentLoop.run_turn(..., memory_context=snapshot) # 所有迭代复用
  → SqliteSessionStore.commit_turn(session, turn)    # 事实与 review job 一个事务
  → provider.sync_turn / Reviewer 从 outbox 消费     # at-least-once + turn_id 幂等；只调 Manager.apply
```

## 当前状态（2026-07-27）

Slice 1–14 验收通过（方案 §14 全部 17 条标准 PASS，125 tests 全绿）。Slice 15（环境快照/精确复现）与 FTS5/向量检索为方案明确的后置阶段；4 个已知缺口见方案 §17。
