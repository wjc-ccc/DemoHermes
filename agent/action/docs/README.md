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

## 启用与普通对话

```powershell
python main.py --cli --use-action
python main.py --use-action
python main.py --no-use-memory --use-action
```

程序装配使用 `build_gateway(use_action=True)`。默认 False，不加载 action，不创建 action 表。action-only 模式仍写入 data/agent.db，但会话保持内存模式，Memory 工具和后台提炼不启用。

按任务收窄工具集：

```python
from agent.action.config import ActionConfig
from agent.action.policy import Toolset
from agent.evolution.assembly import AssemblyPolicy

config = ActionConfig(toolsets=(
    Toolset(name="default", tools=("calculator", "use_skill")),
    Toolset(name="empty", tools=()),
))
gateway = build_gateway(use_action=True, action_config=config)
# AssemblyPolicy 整体传给 run_turn；不要把 toolset 等字段拆开传。
gateway.loop.run_turn(session, user_message, assembly_policy=AssemblyPolicy(toolset="empty"))
```

工具级调用预算、发送尝试和恢复查询分别计算。模型提供的 `_scope/_skills/_source_message_id` 等内部字段会被拒绝；adapter 在校验后注入真实上下文。

可用 `ActionConfig.agent_toolsets={"main": ("default", "empty")}` 将工具集绑定到具体 Agent；启用该映射后，未绑定 Agent 或工具集均拒绝。任务通过 AssemblyPolicy 在 Agent 的允许集合内继续收窄。

## 工具如何接入

1. 在原 tools/skills 中实现 `Tool.execute -> ToolCallResult`，保留业务模块的最终权限/版本校验。
2. 在 action/bootstrap.py 注册 ToolPolicy 与 adapter；现有六类工具已有定级，bash/file_write 不自动注册。
3. 需要新的资源定位或查单格式时，在 action/adapters 内实现 adapter，不在 Loop 添加按工具名的控制分支。
4. high/critical 写工具必须有同工具集中的只读 recovery_tool。外部请求需真实的下游幂等支持，不能只靠本地 UNIQUE。
5. 自定义 POST 返回 GateDecision，effective_value 是完整 ToolCallResult 的 JSON 值，遮蔽时同时处理 data 和 result_text。

自定义 PRE 以 `pre_guards=(callable, ...)` 注册，接收有效参数与 ToolContext，返回 GateDecision；effective_value 为公共工具参数字典。守卫必须无副作用且参数规范化应幂等：执行器会在产生副作用前重新检查，参数发生变化则阻断。本次请求、规范化参数及两侧输出各自留档。

契约冻结包含 schema、policy、操作策略、实现版本、方法代码指纹和 POST 守卫指纹。依赖或配置行为变化仍需更新 implementation_version/守卫 version；代码指纹不是任意语义漂移的自动证明。

恢复查询返回 `data.status="succeeded"` 与原操作回执 `data.result`；明确无效果的终态返回 `status="failed", confirmed_no_effect=true`。not_found/pending/异常保持 UNKNOWN。recover 只查证，不自动重发或补偿。超时的线程可能继续运行，资源占用不能仅因租约到期而解除。

显式补偿入口是 `runtime.recovery.compensate(entry_id, context, arguments)`，补偿工具必须声明原动作 external_ref/idempotency_key 参数，并经过完整执行控制。补偿另有动作记录，成功不覆盖原动作成功；不确定补偿先对账，不能再次发起。

## 计划、提示链与验收

```python
from agent.action.plan.plan import Plan, PlanStep
from agent.action.chain.chain import ChainSegment
from agent.core.memory import build_memory_scope

scope = build_memory_scope(source)
plan = gateway.create_plan(Plan(
    scope_key=scope.storage_key, actor_id=scope.user_id, goal="生成并检查报告",
    steps=(PlanStep(step_id="report", goal="生成报告", toolset="default",
                    chain=(ChainSegment(segment_id="draft", prompt="生成报告"),)),),
), source)
result = gateway.run_plan(plan.plan_id, source)
```

显式计划要求 use_memory=True；Gateway.run_plan 在 Loop 外调度，并等待每段 turn 的持久化提交。库级 run_plan 的 callback 接口为 `run_step(step, segment, prompt, assembly_policy)`，返回已提交的 TurnResult 或 Gateway 的 OutboundReply。伪造成功回调但没有对应持久化 turn 会在来源闸门被拒绝。

每个段声明 output_schema、semantic_gates、fact_gates。任务专用规则通过 `runtime.gates.register(name, callable, layer="semantics"或"facts", version="1")` 注册，callable 接受 Artifact，返回 GateDecision。未注册的规则会阻断。

记忆写入计划还须绑定 source_message_id，指向同 scope 已保存的真实用户消息。生成的计划提示标为 system_injected，不能冒充用户授权。卡片最终提交使用冻结的真实用户来源，而不是链段生成提示。

语法/结构与来源存在检查由模块执行；来源存在不证明金额或叙述真实，核算与业务事实须注册任务专用规则。副作用目标步骤必须指定 target_ref、target_version（适用时）及 required_effects 工具名单，缺少对应成功业务动作不能 DONE。

被拒绝工件和每次判定保留。消费工件使用 artifact_id+content_hash，并重新检查适用闸门。修复产出新工件；retry 仅在没有已发生写效果时有限重跑本段。计划修复保留已验收前段，禁止覆盖 DONE、删除范围外依赖或修改未对账 UNKNOWN。

## 表与证据

action_turns 保存包括空工具集在内的 turn 授权快照；action_contracts 保存逐工具契约。action_items 为业务动作，action_attempts 为每次模型调用，action_events 记录变化，action_observations 记录读版本，action_resource_claims 记录尚未查清的资源占用，action_approvals 绑定参数/对象/版本/契约。

请求参数和有效参数、原始输出和可发布输出分别保存。超过 8 KiB 的只读结果在 action 账本保存哈希，完整可发布内容仍由原会话事实保存；读调用不跨 turn 缓存。raw_output 不自动回注给模型。审批接口是可信应用接口 `runtime.approve(entry_id, actor_id=..., expires_at=UTC毫秒)`，审批后再次执行完整 PRE。
