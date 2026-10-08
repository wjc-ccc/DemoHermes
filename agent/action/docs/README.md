# Action 执行模块

Action 为可选执行控制层。业务工具仍在 tools/skills，模型循环仍在 core，所有新增策略、账本、计划、链段、恢复与 SQL 在本目录。

## 类别与存储

| 类别 | 定义位置 | 是否存库 |
| --- | --- | --- |
| ToolPolicy / Toolset | policy.py | 定义在代码；每个 turn 保存实际契约/授权快照 |
| ToolContext | context.py | 对象只在本 turn；身份、scope、版本等证据入账 |
| ActionItem / ActionAttemptItem | ledger.py | action_items / action_attempts |
| 状态变化、资源占用 | storage/tool_store.py | action_events / action_resource_claims |
| 读取版本、审批 | storage/tool_store.py | action_observations / action_approvals |
| GateDecision | decision.py | PRE/POST 在账本；链段判定在工件库 |
| ActionTrace | action_trace.py | 引用持久账本；模型解释不等于执行证据 |
| Plan / PlanStep / PlanPatch | plan/ | action_plans / action_steps / action_patches |
| Artifact | chain/ | action_artifacts / action_gate_decisions |
| Store / Guard / Adapter 协议 | types/interfaces.py | 不存库 |
| 线程、锁、工具实例 | 执行器/adapter | 不存对象；恢复所需领取凭证入账 |

SQL 时间为 UTC Unix 毫秒。动作的 effect_state、publish_state、compensation_state 分开保存。

迁移由 RuntimeStorage.open(extra_schema_dirs=...) 统一加载，原迁移不改动。更多接入见 INTEGRATION.md，回退见 ROLLBACK.md。
