# 跨模块数据

共享结构、类别、结果与 Protocol 位于 agent/types/，不加载 core、Store、Worker、SDK 或装配。

InboundEvent → Message/Session → TurnContext → ToolTurnContext/ToolInvocationContext → ToolDispatchItem → ToolCallResult/ToolCall → TurnRecord/TurnCommitResult。

MemorySnapshot、SkillSelection、PrecisionResult 表达快照与结果；MemoryMutationItem 表达暂存修改，ActionExecutionEvidence 表达账本凭证。

Runtime 类型归 runtime.py，AssemblyPolicy 归 Action，AuditEntry 归存储，ReplayGateResult 归 Eval。内部 ContextItem、Precision 阶段留在实现，IO 和流程不进入 types。见 [协议索引](../../agent/types/README.md)。
