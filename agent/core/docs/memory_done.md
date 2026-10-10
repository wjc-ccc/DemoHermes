# Memory 当前运行链路与清理结果（2026-09-28）

## 总览

Memory 现在有四种不同生命周期的内容：原始事实、常驻 Markdown、选择性卡片、短期 Session/Anchor。它们共用用户 scope，但不互相复制。新卡只能由后台从**正常完成**的轮次生成；`MEMORY.md` 是人和模型可编辑的短手册，不自动同步为卡片，卡片也不自动写回手册。

| 内容 | 保存位置 | 读取时机 | 在上下文中的角色 |
| --- | --- | --- | --- |
| 原始对话、工具调用、轮次 | `data/agent.db` 的 `sessions`、`turns`、`messages`、`tool_calls` | `memory_search` 找引用，`memory_read` 按需读原文 | 默认不整体注入；是证据和历史来源 |
| Policy、Project、用户手册 | `data/memory/POLICY.md`、项目根目录中的项目文档、`data/memory/<scope_key>/MEMORY.md` | 每轮 `MemoryContextLoader.freeze` | `memory_manual`，Precision P0；Policy/Project/User 用标签区分来源 |
| 用户卡片 | `memory_cards`、`memory_experiences`、`memory_evidence`、`memory_terms`、`memory_card_versions` | 经验关键词最多召回 3 张，其他卡片按需搜索 | 自动经验是 Precision P1；工具结果按普通工具内容进入模型 |
| 当前 Session 与 Anchor | `sessions.payload_json`，消息原文另存上述事实表 | 本轮从当前会话加载；历史过长时 Precision 压缩 | 最近对话保留细节，Anchor 用 intent、changes、decisions、next_steps、excluded 五槽概括旧片段 |

文件路径由 `agent/main.py::_memory_paths` 决定；默认 `data/agent.db` 与 `data/memory/`。用户目录以 `MemoryScope.storage_key` 命名，键由组织、租户、用户和项目的稳定身份生成，不含临时 session/turn。群聊与私聊保留不同 scope；工具调用的 scope 由运行时注入。

## 一个正常轮次如何流过 Loop

1. Gateway 根据渠道来源取得 scope，`MemoryContextLoader.freeze` 读取 Policy、Project、当前用户的有效 `MEMORY.md`。用户手册默认最多约 1200 个估算 tokens；非法或超长的人工版本保留在磁盘，但上下文使用最近有效版本并报告诊断。
2. Retriever 对当前输入做关键词匹配，只允许 `reusable_experience` 的有效卡进入自动经验区；最多 3 张，总预算默认 600 tokens，完全不匹配时为空。本轮快照固定，不受后台随后写入影响。
3. Precision 把系统提示、技能和 Markdown 快照放入 P0，把经验放入 P1。当前 Session 消息和 Anchor 在历史层级中处理；生产入口启用上下文总预算，旧对话达到阈值时按五槽压缩。原始消息不会因为上下文压缩而从 SQLite 删除。
4. AgentLoop 调用模型与工具。`memory_search` 目前只提供关键词模式，可检索卡片、历史消息、工具原文和已登记文档；`memory_read` 根据引用分页读详情。语义和混合检索仍是扩展位置，不作为当前可调用能力。
5. `memory_edit` 可更正或删除**已有**卡片，必须提供 `item_id` 与 `expected_revision`；也可独立读取、替换、恢复用户手册。卡片更正/删除先暂存为工具结果，只在该轮正常完成时由 SessionStore 同事务提交。失败或中断的轮次仍保留原文，但不执行卡片修改。
6. SessionStore 持久化完整轮次和工具结果，Scheduler 只为成功完成的轮次计数。用户 scope 累计第 1、3、6、10、15、20……轮形成固定来源批次；`/reset` 和正常退出补排剩余轮次，不推进间隔阶段。
7. 后台 Worker 读取批次的固定 turn ID 和原始 Session，`MemoryExtractor.extract_batch` 调用一次 LLM，返回零张或多张提案。`[]` 是正常空结果；调用异常或无效 JSON 使 Job 有限重试。单张提案格式错误只拒该张，其他提案继续审查。
8. Reviewer 对逐字引文、来源范围、词语与实体数字锚点、经验结果极性做校验；Validator 对四类字段、敏感内容、长度和关键词做结构校验。Committer 在同一事务处理去重、同槽位更正、删除保护、卡片版本、Trace 与 Job 完成。终止失败或取消的旧批次不阻塞后续批次；旧证据仍不能覆盖较新明确修改。

## 卡片定义

卡片只有一个分类字段 `kind`，与原始消息的来源类型或 Markdown 的层级标签不是一一对应关系。`memory_search`、`memory_read` 和 `memory_edit` 的工具结果不会再作为新卡证据，避免旧记忆被复制成新卡。

| `kind` | 准入对象 | 默认读取 |
| --- | --- | --- |
| `profile` | 对未来理解用户长期有价值的身份、能力、关系 | 工具搜索 |
| `stable_preference` | 明确且长期的表达方式、习惯、选择；一次任务要求不泛化 | 工具搜索 |
| `ongoing_fact` | 有对象、可被后续事实更正的持续项目背景、事项、约定 | 工具搜索 |
| `reusable_experience` | 在特定条件下采取做法后真实观察到的结果及限制 | 具体关键词命中时自动召回，也可工具搜索 |

前三类用 `subject + attribute` 生成稳定事实槽位，便于更正；经验不占槽位，按观察内容去重。每张卡只表达一条可独立更正的事实，或一次有完整证据的经验。经验需填写适用条件、做法、结果、限制、success/failure/mixed/observed 标识和 3–8 个具体关键词。失败教训可保存，但不能改写为成功。每张卡须引用本批用户消息或工具原文，并提供逐字引文；助手回复只帮助理解上下文。寒暄、临时请求、待办、过程流水和助手自行声称的事实不入卡。

`active` 表示可使用，不等于“用户已确认”。来源权威仍区分用户明示、工具验证和模型推断。模型自报置信度只保留为元数据，不替代来源与证据检查。同一槽位新明确更正可替代旧卡；弱证据或无法裁决的冲突进入 `memory_conflicts`。删除保留 tombstone 与版本历史，旧批次不能恢复旧值。

## Markdown 边界与维护

`POLICY.md` 和项目文档由运行环境提供约束，`memory_edit` 无权修改。用户的 `MEMORY.md` 保存少量稳定资料和沟通约定，支持普通 Markdown；人可直接编辑，模型通过 `manual_get`、`manual_replace`、`manual_restore` 编辑。编辑器检查 scope、长度、敏感内容、预期哈希并登记版本。人工外部修改在下一次读取时登记；无效磁盘内容不会覆盖有效上下文。手册变更不产生卡片 Job，后台新卡也不修改手册。

五层 `MemoryLayer` 继续描述历史来源和文档权威。Task 枚举只为历史 Trace 保留；Task 三态工作台、叙事、精确状态、存储接口与调试页面已下线。普通 Session 和 Anchor 仍运行。

## 数据升级与边界

`005_memory_cleanup.sql` 不修改已应用迁移，取消遗留手册同步/维护 Job，接管未完成的旧提炼 Job，隔离只有手册证据而没有对话/工具证据的旧卡，删除手册同步状态及 Task 专用表。手册版本、原始事实、卡片、证据、审计和 Trace 保留。执行到现有数据库前已用 SQLite backup 保存 `data/agent.pre_memory_cleanup_20260928.db`。

当前关键词召回依赖具体对象和场景词，不能等同语义理解。逐字引文及实体锚点只能降低编造风险，不能完整证明一条自然语言主张被原文蕴含。模型可以选择不产卡，`[]` 不被规则兜底填充；这正是选择性记忆的边界。

主要实现入口：`agent/main.py`、`agent/core/memory/memory_context_loader.py`、`agent/core/memory/memory_extractor.py`、`agent/core/memory/memory_batch_reviewer.py`、`agent/storage/sqlite/memory_review_scheduler.py`、`agent/storage/sqlite/memory_review_committer.py`、`agent/tools/memory_edit_tool.py`。


## 统一 Markdown 文档入口

`MarkdownDocuments` 统一读取 Policy、Project 和 User 文档。只读路径来自启动配置，个人 MEMORY.md 根据可信运行期 scope 定位。`read(..., persist=False)` 与正常读取共用校验/回退，不写修订历史；检索、冻结、memory_edit、Action 资源定位共享同一生产实例。

AI 仅通过 replace_user/restore_user 编辑当前个人 MEMORY.md；工具不接受路径或目标身份参数。保留内容预算、凭证过滤、路径隔离、expected_hash 冲突检查、版本恢复及数据库失败后的文件回滚。context_documents 保存索引，memory_document_versions 保存有效历史正文。旧 MarkdownBook/MemoryManualEditor 与 ManualRead 已移除，DocumentRead 只保存 snapshot/diagnostics/disk_hash/revision。
