# AgentLoop

Runtime 注入模型、Precision、ToolHandler、Skills、AgentLoopTrace 和 LoopConfig。

run_turn(session, user_message, *, memory_snapshot, memory_scope, ephemeral_prompt, assembly_policy) → TurnResult 保留。Loop 不解析平台协议，不提交事务。

身份与用户事实 → Handler 冻结授权 → 技能选择 → Precision → 模型 → Handler/Dispatch → 工具事实及回注 → 完成或上限。

Loop 不按模式或工具名称分支，成功结果的 readback_reference 触发准入纠偏。模型重试及失败语义保持。Precision 阶段状态不成为 Loop 接口；Loop 消费 PrecisionResult，更新 Anchor 和定案。Trace 保留完整输入与 schema，失败构建不复用旧快照。
