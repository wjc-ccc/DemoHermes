# AgentRuntime 当前架构

本文对应 2026-10-10 工作区。历史方案不代表当前已启用能力。

## 装配与职责

main.py → agent.main.main → agent.runtime.build_runtime(RuntimeConfig) → AgentRuntime。

Runtime 装配模型、SQLite、技能与工具、可选 MemoryBuilder、ToolHandler/ToolDispatch、PrecisionBuilder、AgentLoop 和 Gateway。core 保留原路径，属于 Runtime 内部能力。

| 模块 | 行为 | 实现 |
| --- | --- | --- |
| AgentRuntime | 装配、启停、能力说明、提交后通知 | agent/runtime.py |
| Gateway | 路由、会话排队、冻结 Memory、提交与回复 | agent/gateway/gateway.py |
| AgentLoop | 技能冻结、模型迭代、状态更新、工具事实回注 | agent/core/agent_loop.py |
| MemoryBuilder | 装配快照加载器、工具和 Reviewer | agent/core/memory/memory_builder.py |
| ToolHandler | PRE、审批、版本、预算、资源领取及分配 | agent/core/tool_handler.py |
| ToolDispatch | 调用、超时、迟到结果、效果、POST 与回放 | agent/core/tool_dispatch.py |
| PrecisionBuilder | 保护、准入、Anchor、渲染及预算 | agent/core/precision/percision_engine.py |
| Trace | 事件载荷、完整输入快照、指标窗口 | agent/core/agent_loop_trace.py、precision/percision_trace.py |
| RuntimeStorage | SQLite 初始化与 Store 装配 | agent/storage/sqlite/runtime_storage.py |

## 一轮请求

    Channel → Gateway 入站队列 → 同会话排队
      → MemoryContextLoader.freeze（启用时）
      → AgentLoop.run_turn
          → Handler.begin_turn：冻结授权工具、契约、计划凭证
          → SkillRegistry.select：本轮选择一次，正文冻结
          → PrecisionBuilder.build → model.chat
          → 有工具请求：Handler 审批/分配 → Dispatch 执行
          → ToolCall 追加 → 回读纠偏 → tool 回注 → 下一次模型调用
      → SessionStore.commit_turn
      → 提交成功：Reviewer 通知、环境快照
      → 出站队列 → Channel

基础模式保留错误回注。Action 保留两阶段 PRE、审批绑定、版本和调用预算、幂等领取、UNKNOWN、迟到回执、恢复与补偿。连续只读调用可并行，写调用顺序分配；后续审批在前序结果之后进行。

## 数据与事务

共享结构、类别、结果和 Protocol 归 agent/types/，不加载 core、Store、Worker 或 SDK。

TurnContext 是本轮身份，ToolTurnContext 是授权快照，ToolInvocationContext 是可信工具上下文，ToolDispatchItem 是执行、回放或拒绝指令。模型参数不决定真实 scope、技能快照和来源。

ToolCallResult.pending_mutation → ToolCall.pending_mutation 是卡片暂存指令；action_evidence 是领取与账本证据。HTTP/Debug 经 observation_payload() 保持传输字段。

提交顺序：事实 → Action 凭证和冻结指令检查 → memory_edit 修改与回执 → 清理未应用暂存、记录 review outbox → SQLite 提交 → Reviewer 通知。TurnCommitResult 返回持久化与卡片回执。手册、技能正文保留现有执行时机。

SQL 检索和文档版本实现位于 storage/sqlite/memory_retrieval.py、markdown_documents.py；关键词与 token 估算纯函数在 core/memory。存储版本由契约及装配提供，不导入 Extractor 实现。

Precision 内部经过保护、准入、Anchor 规划、历史选择和完成阶段，阶段结果留在实现。最近两轮原文、优先级、原始历史 digest、失败回退与回读纠偏保持。

## 开关与生命周期

| Memory | Action | 会话存储 | 执行 | Reviewer/预算 |
| --- | --- | --- | --- | --- |
| 关 | 关 | SQLite | 基础 | 关闭 |
| 开 | 关 | SQLite | 基础 | 开启 |
| 关 | 开 | SQLite | 受控 | 关闭 |
| 开 | 开 | SQLite | 受控 | 开启 |

会话存储独立于 Memory 和 Action 开关：所有组合都将会话、消息、轮次及工具事实提交到 SQLite，重启后恢复原会话。Memory 关闭时不加载记忆快照、记忆工具、Reviewer 与上下文预算；正常对话历史仍然恢复并参与后续请求。Action 关闭时不加载 Action 实现、不建立 Action 表。

默认 Memory 开、Action 关。默认 calculator、use_skill、skill_manage；Memory 开时增加 memory_search/read/edit。bash/file 不默认注册。

启动：Action 查证 → Gateway → Reviewer → 入口启动渠道。停止：Gateway/渠道 → Memory 刷新与处理 → Dispatch 资源。每个 Runtime 拥有独立 EventBus、PrecisionTrace。事件只用于观测，不能替代事务通知或作业。

## 技能、评测与维护

普通技能选择、冻结、use_skill、skill_manage、版本、备份和审计保留。新建 draft 通过可信管理入口审查启用，Runtime 不自动晋升。

Skill Evolution、反思、蒸馏、优化、候选 Shadow、晋升 Worker、专用作业及实现包已删除。SkillSelector 归 skills，AssemblyPolicy 归 Action 类型；通用 Eval、报告比较、消融、分类和 Memory 评测保留。

当前没有工作台、SqliteWorkspaceStore 或 Plugin 加载器。计划、链段、工件验收与恢复属于可选 Action。

先读 Runtime 装配与 Loop 迭代，再进入 Handler 审批、Dispatch 副作用、Store 事务、Precision 取舍。Trace 区分运行核验与 AI 输出。

新库按原迁移初始化，Action 表按开关加载。本次切换仅重建运行库及 WAL/SHM，保留手册、技能和附件；正常启动不删库。
