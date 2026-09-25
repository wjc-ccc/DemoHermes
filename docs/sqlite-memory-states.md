# SQLite 与 Memory 三态（2026-09-24）

生产入口：`RuntimeStorage.open("data/agent.db")`。业务代码只调用 Store，迁移仅在启动入口执行。测试和评测可传入隔离数据库路径。旧库不自动迁移，也不会在启动时自动删除。

## 表目录

| 表 | 含义 |
|---|---|
| sessions | 会话、入口路由、当前标记；payload 保留原始 context_anchor |
| turns | 每轮不可变事实、Precision 原始载荷、环境快照与可选用量 |
| messages | 正文、角色、会话内顺序和所属轮次 |
| tool_calls | 工具请求、结果和可选节点、来源引用 |
| session_workspaces | 调度态总体状态、计划和契约版本、当前节点 |
| workspace_items | DAG 节点、分工、依赖、输入引用和显式执行状态 |
| goal_contract_versions | 目标契约的追加版本，记录用户消息、修改者、原因 |
| narrative_events | 只追加叙事账本；提炼内容作为 proposed observation |
| session_state_items | 精确值、作用域、版本、有效性及来源 |
| memory_items | 当前长期记忆卡片，内嵌 keywords / evidence |
| memory_item_versions | 卡片与证据的完整历史快照 |
| context_documents | Markdown 路径、作用域、hash、token 等登记 |
| jobs | 通用作业、去重、领取、续租、失败、重试、取消和提交结果 |
| traces | Memory 等模块的操作摘要 |
| trace_items | 原子观测，分离 ai_json / runtime_json |
| audit_events | 技能等管理变更审计 |
| schema_migrations | 已应用的结构版本和校验和 |

SQL 索引时间字段使用 UTC Unix 毫秒；领域 JSON 保留原有时间格式和精度。关联 ID、状态、顺序、版本独立成列；嵌套结构保留明确领域类型。Precision 仍保存在原有 turns.payload_json.precision_trace。

## 统一接口

```python
from agent.storage.sqlite import RuntimeStorage

storage = RuntimeStorage.open("data/agent.db")
# storage.sessions / executions / memory / documents / jobs
# storage.workspaces / narrative / state / traces / audit / observe
```

会话重开在一个事务内取消旧会话的当前标记并创建新会话；局部唯一索引保证每条 session_key 最多一个当前会话。事实仍在现有 Gateway 提交时机落库，事务同时写入 Memory 提炼 Job。

## 三态的显式操作

实体位于 `agent/core/memory/session_workspace.py`、`session_narrative.py`、`session_state.py`；Protocol 在 `memory/types/session_store_types.py`。

- 调度态：`workspaces.create`、`get`、`get_item`、`replace_plan`、`set_item_status`、`set_status`。计划变更校验聚合 revision；节点变更校验节点 revision。校验依赖存在、同一会话、无环。删去的节点保留为 cancelled，以维持历史引用。操作不会启动执行者或自动推进节点。
- 叙事态：`narrative.append_contract`、`append`、`get_contract`、`list_contracts`、`list_events`。契约需要用户消息引用、修改者和原因；更正账本应追加新事件。
- 工作集：`build_working_set(session_id, contract_revision=..., item_id=..., event_ids=(...), state_refs=(...))`。在同一只读事务中按指定引用加载，不自动选材、蒸馏或注入上下文。
- 机械态：`state.put(item, expected_revision=n)` 追加 n+1 版本；`get(ref)`、`get_current`、`list_history`、`list_current` 查询；`invalidate(ref, reason=..., actor=...)` 追加失效版本。最新版本失效时仍返回该版本，调用方必须查看 validity，不自动回退。
- StateRef 固定 session_id、scope_key、key、revision；Provenance 保存来源类型、来源 ID、路径、父版本引用。金额 decimal 使用字符串，避免浮点误差。

这些新类型属于 Memory，与现有 Anchor 独立。未接入长程任务模式、AgentLoop、工具参数解析、审批、channels 或新的校验器策略。

## 长期记忆与后台作业

`memory.put(item, expected_revision=n)` 同时写当前卡片和完整历史。关键词和证据更新也必须提升 revision；支持 get、search、history、soft_delete。重复事实合并证据时保留旧版本。

Memory Review 使用 `memory.extract`，Skill Replay 使用 `skill.evaluate`。Job 保存 handler_version、类型化 input、去重键和 result；领取生成新 lease_token，续租、失败和完成均检查凭证。Memory 的卡片、叙事观察、Trace 与 Job 完成在同一事务提交，任一步失败全部回滚；同凭证同结果重复提交返回既有结果。

Skill 评测算法不变。涉及文件变更的 Skill Replay 不自动抢占过期运行任务，避免重复产生外部副作用；需要查看执行结果后显式取消及重试。普通可重入 Job 与 Memory Review 支持领取过期任务。后台 Worker 定期续租。

## 前端与观察

以 `python main.py --enable-memory-debug` 启动后：

- 数据库表：17 张表的分组、字段说明、分页行和 JSON 详情。
- 模式指标：Memory 统一 Trace 和原路径 Precision Trace；在 SQL 层分页。
- 会话三态：输入 session_id 查看任务分工、当前契约、分页账本、精确值与 Provenance。
- `GET /api/debug/memory/states?session_id=...&limit=50&offset=0` 为只读投影。
- 工作集需显式传引用通过 Store 加载，没有独立工作集表。
- 观察连接使用 SQLite mode=ro；上下文预览不注册文档、不创建 scope 目录。

## 本轮未启用的运行机制

三态默认具备存储能力，但不会因普通对话自动创建工作台。后续再由上层显式启用长程任务并调用这些接口。本轮不实现验证闸门、漂移哨兵、复诵、自动模式升级、工具参数拦截或失效传播。
