# 数据库字段契约与首批清理

日期：2026-10-11。此表按当前生产写入/读取链路整理。开发库已备份重建并发布五个题库版本；完整真实模型评测仍在修补复跑。

| 内容 | 产生位置 | 保存位置 | 实际消费者/验证 |
|---|---|---|---|
| 会话路由、归属、生命周期和 Anchor/准入状态 | Gateway、Session、Perception | sessions 查询列与 session_json | SessionStore 恢复、会话列表、Scope 检索 |
| 完整消息、附件 ContentPart、顺序、角色和来源 metadata | building_messages、Session | messages 查询列与 message_json | 会话重启恢复、原文检索、证据校验、Debug |
| 轮次成功/失败/中断、时间和完整错误 | AgentLoop、Gateway | turns.finished_at/error_text/turn_json | 轮次详情、Memory 调度、失败轮次回归 |
| 本轮环境快照 | EnvironmentSnapshot | turns.environment_json | 轮次 Debug 和环境读回 |
| 每次 Perception 构建、实际请求消息/工具、准入投影和读回 | PerceptionTrace、AgentLoop | turn_json.perception_trace.builds | 完整迭代恢复、模型请求对照、Trace 统计 |
| 每次主模型尝试的原始 usage、时间、错误、结束原因和返回模型标识 | 模型 Adapter、AgentLoop | builds.model_calls | 用量聚合、重试定位和重启恢复；缺失用量 NULL |
| Anchor 辅助调用与失败用量 | ModelAnchorBuilder | builds.auxiliary_calls | 模型调用录制与 Trace 对照、累计开销 |
| 工具原始参数与解析参数 | 模型 Adapter、ToolDispatch | tool_call_json.arguments_raw/arguments | 原始参数留档、Action 语法校验和工具执行 |
| 工具持久事实 ID 与模型协议 ID | ToolHandler、ToolDispatch | tool_calls.tool_call_id；tool_call_json.provider_tool_call_id | SQL 原文读回、工具协议配对、重复 ID 回归 |
| 工具结构化结果、文本结果、错误和耗时 | 工具、ToolDispatch | tool_call_json.result_data/result_text/error/duration_ms | 机器字段读取、文本回注、原文检索和 Debug |
| Action 效果/发布状态、尝试和待提交修改 | Action、ToolDispatch | Action 账本；tool_call_json 的 action_evidence/action_trace/pending_mutation | 幂等、恢复、正常轮次原子提交和状态核验 |
| 卡片、适用条件、事实槽位和当前修订 | Extractor、Reviewer、Validator、Committer | memory_cards、memory_experiences | 检索、卡片读回、条件判断和乐观并发 |
| 证据原文、原文哈希、观察时间和关键词 | Reviewer、Committer | memory_evidence、memory_terms | 原文核验、检索、Scope 与来源追踪 |
| 卡片历史、冲突和明确更正/删除 | Committer、MemoryMutation | memory_card_versions、memory_conflicts | 旧证据防恢复、冲突处理和历史读回 |
| 提炼批次、租约、重试和已提交结果 | ReviewScheduler、JobStore | memory_review_turns、jobs | 固定来源回放、幂等和失败区分 |
| 保存理由、未来用途和跳过原因 | Memory v11提炼及独立准入复核 | Memory Trace 的 ai 字段；Job 审查结果 | 建卡判别审计；不新增闲置卡片列 |
| 继承阶段期限的已有卡及版本 | 提炼模型、运行时校验 | memory_cards.metadata_json.validity_source | 同scope/修订号/截止时间提交校验；防止期限取值漂移 |
| 失败辅助模型调用及未解析响应 | MemoryExtractionError、Worker | jobs.result_json.failure_history | 每次失败attempt/usage/原始响应；重试成功后继续保留 |
| 文档路径、范围、指纹、成本和装载时间 | DocumentRegistry | context_documents | 注册文档检索、指纹检查和调试 |
| 评测内容版本、问题契约版本与内容哈希 | EvalDataset、SqliteEvalStore | eval_datasets/cases/questions | 同版异内容拒绝、按版本读回、历史前缀校验 |
| 实际回答、实现版本、问题指纹和语义复核历史 | 评测运行入口 | eval_runs/results | 回答不能改写、结果绑定已发布问题；逐题隔离运行及失败复跑 |

SQL 中有消费者的查询列和完整领域 JSON 都保留：前者支持筛选/关联/索引，后者支持完整恢复和审计。

## 首批变更

- context_documents 删除 revision_ref：生产登记没有写入或读取。
- context_documents 删除 tokenizer_name：生产始终登记 unknown，没有实际读取消费者。
- Session.message_count 删除独立状态，从消息列表派生；数据库会话列表使用 SQL COUNT。
- 核心 payload_json 分别改为 session_json、turn_json、message_json、tool_call_json；评测使用 dataset_json、case_json、question_json。
- turns.completed_at 改为 finished_at；完整错误字符串 error_code 改为 error_text。
- ToolCall.durations_ms/tool_call_result 改为 duration_ms/result_data，旧数据字段仅在解析入口兼容。
- 评测表统一为 eval_*；问题内容版本随 dataset_version 发布，另存 question_schema_version，未增加独立问题内容版本。
- input_token_count/output_token_count 属于应保存但原来未写入的字段，现已补齐写入，保留 NULL 的准确含义。

迁移通过010_storage_contract执行，001–009已有迁移文件不改写。开发库已重建，备份为data/agent.pre_long_repair.20261011-034339.db；新库包含91个场景、140道问题，已导入已完成的评测结果。

长对话存储审计入口：`python scripts/audit_long_dialogue_storage.py <运行目录>`。逐题对照冻结的历史前缀、数据库消息原文、完整工具文本/结构化结果/状态、实际 Trace 和 usage 列；报告保存为该运行目录的 `storage-audit.json`。
