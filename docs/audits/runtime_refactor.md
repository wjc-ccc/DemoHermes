# Runtime 架构重构验收记录

## 实施基线

- 以本次开始时的工作区为基线，保留已有未提交改动，包括正在迁入 Python 模块的提示词。
- 初始后端测试无法收集：53 个错误，主要是提示词导入与 agent 包实际位置不符，以及仍导入已删除微信 QR 脚本的测试。
- 初始前端测试与构建受到 Windows 沙箱 realpath EPERM 限制；后续重新验证。
- 提示词消费者直接改用现有 agent.prompt.prompts；过期 QR 脚本测试随已删除实现移除，渠道测试继续保留。

## 完整验收范围

1. 删除 Skill Evolution、专用 Worker、作业、Shadow、信号、提示词和测试；迁回技能选择、评测、环境快照、审计及计划策略。
2. AgentRuntime 拥有装配、生命周期和提交后通知，MemoryBuilder 装配记忆能力。
3. ToolHandler 承担执行前审批和分配，ToolDispatch 承担执行、超时、效果、POST 与回放。
4. 可信工具上下文、待提交修改、执行凭证与提交回执使用 types 契约；types 独立导入。
5. Perception 阶段交接和观测收拢，Memory SQL 移至 SQLite 实现。
6. 四种开关组合、并发、事务、Action 恢复及计划、Memory、技能、Perception、HTTP/SSE、评测和前端。
7. 当前文档对应真实入口；切换新库限定为运行数据库，保留手册、技能和附件。

## 最终验收（2026-10-10）

| 要求 | 当前证据 |
| --- | --- |
| 完整删除 Skill Evolution | evolution 包、类型文件、专用 Shadow/Worker/作业和测试已删除；运行源码无相关导入或触发 |
| 保留普通技能 | selector、Registry、Repository、use_skill、skill_manage 保留；创建 draft、可信管理启用及版本测试通过 |
| Runtime / MemoryBuilder | main 仅选择入口，runtime.py 装配并管理生命周期；MemoryBuilder 返回共享协议定义的结果 |
| Handler / Dispatch | PRE、审批和分配在 Handler，调用、UNKNOWN、迟到回执、POST 与回放在 Dispatch；恢复和补偿使用同一路径 |
| 明确共享契约 | 可信上下文、修改、执行证据、提交回执和 Perception 轮次快照进入 types；独立导入不加载 core 或实现 |
| Perception 阶段 | 保护、准入、Anchor 规划、历史选择、完成交接；完整快照、预算、原文 digest、失败回退与回读测试通过 |
| Memory 存储归属 | SQL 和文档版本实现归 SQLite；core 无直接 SQL，纯关键词和 token 估算保留 |
| 默认值与开关 | Runtime 四组合均持久化会话；关闭 Action 不导入实现、不建 Action 表；Memory 控制记忆装配与生命周期 |
| 生命周期与隔离 | 重复启停、不同 Runtime 独立事件和 Trace、会话串行/跨会话并行测试通过 |
| 事务、恢复和计划 | 卡片与回执原子性、回滚、中断、版本、幂等、UNKNOWN、补偿、计划来源/契约/工件测试通过 |
| HTTP/SSE | 既有 Gateway/HTTP/SSE 测试通过；另以真实本机 HTTP 验证 chat 响应、Action 元数据和内部字段隐藏 |
| 当前文档 | runtime-architecture.md、当前认知文档、Action 与 types 文档校准；入口链接检查通过 |
| 新运行库 | data/agent.db 重新初始化，sessions/turns/messages/tool_calls/memory_cards/jobs 均为 0，Action 表未默认建立，integrity/foreign-key 检查通过 |
| 保留文件 | data/memory、data/skills、data/channel/media 下 9 个文件的切换前后 SHA-256 一致 |

完整后端：390 passed；前端：11 passed；前端生产构建通过。CLI --help、最终语法检查、git diff --check 通过。飞书 SDK 的既有事件循环弃用警告未改动。

测试在正常执行环境运行：沙箱对 SDK 本机 socketpair 和 Vite realpath 的限制已经用堆栈/错误输出确认，不作为业务代码回退依据。初次库切换未正确在停止检查失败后中止，随后明确停止项目后端、重新创建并完成上述空库和文件核验。

原项目后端已停止，未自动启动平台渠道。运行 python main.py 可启动新版；普通启动不删除数据库。

## 会话持久化修正（2026-10-10）

Runtime 始终装配 SQLite，会话、消息、轮次和工具事实落库不再受 Memory / Action 开关影响。Memory 开关继续控制记忆快照、工具、Reviewer 与预算；Action 表继续按 Action 开关装配。

四组合落库、关闭 Memory / Action 后重建 Runtime 恢复完整会话并将旧历史传给模型、失败轮次落库的验收通过。相关测试 23 passed，完整后端 392 passed；CLI --help 与 git diff --check 通过。本次没有重建或清空运行数据库。

## 删除 dict 会话链路与 Action 归位（2026-10-10）

- 删除 agent/gateway/session_store.py、DictSessionStore 导出、Gateway 内存回退与 `_persistent` 分支；store 为必填依赖，测试使用临时 SQLite。
- agent/action 整体迁至 agent/core/action；Runtime、Handler、Dispatch 和测试直接使用新导入路径，共享契约保留在 agent/types/action，没有旧路径包装。
- 20 个 Action 实现文件与迁移前 AST 一致，3 个 SQL 迁移文件字节与 SHA-256 一致；旧 Action 包和 dict 存储模块无法导入。
- 13 处 Gateway 装配调用全部显式提供存储；运行代码与测试无 DictSessionStore 或旧 Action 引用。当前架构、执行接入与 Gateway 文档同步更新。

相关测试 76 passed。首次完整回归发现原超时测试依赖短延迟的时间竞争，改为显式控制模拟银行入账时机，执行代码未改；修正后完整后端 392 passed。CLI --help 与 git diff --check 通过。本次未重建或清空运行数据库。
