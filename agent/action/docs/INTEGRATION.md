# 接入位置

| 标记 | 调用方与时机 | 用途 | 关闭模块 |
| --- | --- | --- | --- |
| ACTION-HOOK-01 | AgentLoop 创建 turn 后、冻结技能前 | begin_turn，建立局部可信上下文 | 不创建上下文 |
| ACTION-HOOK-02 | AgentLoop._freeze_skills | required_tools 使用允许工具集 | 原注册表名单 |
| ACTION-HOOK-03 | AgentLoop._chat_with_retry | 模型只见筛选后的 schema；空集保持为空 | 原 get_schemas |
| ACTION-HOOK-04 | Loop response.has_tool_calls 分支 | execute_batch，记录工具事实并回注 | 原 _execute_tool |
| ACTION-HOOK-05 | RuntimeStorage.open / migrate | 可选 action/schema，统一 checksum | 原迁移目录 |
| ACTION-HOOK-06 | SqliteSessionStore.commit_turn 事务 | 卡片实际写入与回执原子提交 | 无 hook |
| ACTION-HOOK-07 | SkillManageTool patch | expected_revision 送达真实写入口 | 可显式使用业务版本检查 |
| ACTION-HOOK-08 | build_gateway | 启用时装配并注入 ActionRuntime | 不导入 action |
| ACTION-HOOK-09 | Gateway start/stop | 启动查证，停止执行资源 | 原生命周期 |
| ACTION-HOOK-10 | Gateway.run_plan | 外层调度调用正常持久化 turn | 明确拒绝计划入口 |
| ACTION-HOOK-11 | build_tool_message | 区分唯一会话事实 ID 与 provider 回注 ID，避免跨 turn 重名冲突 | 原 tool_call_id |

代码规则：参数校验先于资源读取；可信内部参数不暴露给模型。ActionRuntime 的 ToolContext 每轮局部传递，不放进共享 Loop 属性。ActionToolCatalog.register 用于可信工具注册，未知工具不能自动获得权限。

卡片 ToolCall success 表示暂存已接收；实际成功查看 action_items.effect_state 和 turn_commit_confirmed 事件。事务 hook 不改已入库的不可变 ToolCall。已成功卡片回放不会再带 memory_mutation。

新增工具：实现原 Tool 接口与必要 Adapter，在 bootstrap/caller 的可信注册中声明 ToolPolicy；外部写工具提供真正的下游幂等与只读 recovery_tool。查询输出使用 status=succeeded 或 status=failed + confirmed_no_effect=true；not_found/pending 保留 UNKNOWN。

## 应该把代码写在哪里

| 需求 | 位置 |
| --- | --- |
| 新工具定级、默认工具集 | bootstrap.py / policy.py / config.py |
| 参数/权限/审批/版本前置条件 | guards/pre.py 与具体领域 guard |
| 实际执行参数注入、现有对象适配 | adapters/builtin.py / resources.py |
| 返回值结构、敏感内容发布 | guards/post.py，注册 post_guards |
| 去重、资源领取、次数与状态事件 | storage/tool_store.py，不在 Loop 用内存计数 |
| 业务动作状态与证据字段 | ledger.py |
| 计划 DAG、受影响子图与补丁 | plan/validator.py |
| 调度、完成条件、恢复推进 | plan/runner.py |
| 工件消费、修复与段内重试 | chain/chain.py |
| 任务核算/业务来源验收规则 | 注册到 chain/gates.py，规则实现在本模块的领域代码 |
| SQL 与 Store 实现 | storage/schema 与对应 store；添加新编号，不改已应用迁移 |
| 对外协议 | types/interfaces.py；领域类别保留在所属模块 |

Plan 在创建时冻结契约。PlanRunner 领取凭证随 AssemblyPolicy 传入 begin_turn，工具领取事务再次检查凭证与 scope。步骤 max_iterations 为本次 Loop 的局部上限，不修改共享 Loop.max_iterations。恢复查询不继承过期的步骤发送权。

事务 hook 的 before_commit 在实际卡片修改前调用，record_mutation 与成功写入共用连接，finish_turn 清理未应用暂存。hook 内不运行模型或外部网络调用。
