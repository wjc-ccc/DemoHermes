# Action 当前接入

RuntimeConfig(use_action=True) 装配 ActionRuntime、Handler 和 Dispatch。默认关闭不加载实现或迁移。

实现入口为 agent/core/action/bootstrap.py，执行控制内部实现集中在 agent/core/action，共享协议与数据保持在 agent/types/action。Gateway 的 SQLite 会话存储为必填依赖。

| 时机 | 模块 | 保证 |
| --- | --- | --- |
| Runtime.start | 恢复 | 先查证，再接收请求 |
| Loop 开始 | Handler.begin_turn | 授权、契约、计划凭证只属于本轮 |
| 模型前 | Handler.get_schemas | 只暴露授权工具，空集保持为空 |
| 工具请求 | Handler → Dispatch | PRE/审批/预算/领取后调用，保留 POST 和效果状态 |
| Store 提交 | ActionTurnCommitHook | 修改和回执同事务，核验凭证及冻结指令 |
| Gateway.run_plan | 计划 | 真实持久化轮次，注入提示不能冒充用户来源 |
| 回注 | build_tool_message | provider ID 与事实 ID 分开 |
| Runtime.stop | Dispatch | 停资源，未知效果不重发 |

ToolInvocationContext、ActionExecutionEvidence、pending_mutation 是明确契约；HTTP 投影保留字段。PRE 规范化必须幂等，POST 不抹掉外部成功。恢复只查证，not_found/pending 保留 UNKNOWN。补偿另记动作，计划冻结契约和凭证，局部上限不改变共享 Loop。
