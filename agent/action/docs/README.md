# Action 执行模块

Action 可选。Tool 在 tools/skills，审批分配在 core/tool_handler.py，执行、效果和发布在 core/tool_dispatch.py。账本、Guard、计划、工件和恢复仍在 action/。

    python main.py --cli --use-action
    python main.py --use-action
    python main.py --no-use-memory --use-action

    from agent.runtime import build_runtime
    from agent.types.runtime import RuntimeConfig
    runtime = build_runtime(RuntimeConfig(use_action=True))
    runtime.start()
    # 通过 runtime.gateway 接收消息，Loop 接收完整 AssemblyPolicy。
    runtime.stop()

默认关闭不加载 Action 实现和 Action 表。会话始终持久化到 SQLite；Action 开启时增加账本，Memory 关闭时记忆工具和 Reviewer 关闭。

工具声明 action_policy/action_operations，装配校验；未知工具不自动授权。高风险写入需下游幂等与只读 recovery_tool。PRE 保留权限、审批、版本、观察、契约及预算；Dispatch 保留超时 UNKNOWN、迟到回执、效果与 POST。

计划类型在 agent/types/action。gateway.create_plan/run_plan 复用真实提交轮次，记忆写入须真实 source_message_id，注入提示不能授权。工件、语义、来源和 required_effects 满足才 DONE。

恢复与补偿走同一 Handler/Dispatch。UNKNOWN 不重发，补偿保留原成功。卡片工具 success 是暂存，实际成功看事务回执，回放不再交出指令。

迁移由 RuntimeStorage.open(extra_schema_dirs=...) 加载，原 checksum 不改。接入见 INTEGRATION.md，历史回退见 ROLLBACK.md。
