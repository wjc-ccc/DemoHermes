# Tool、Skill 与 Plugin

注册以 agent/runtime.py:build_runtime 为准。Registry 管定义，Handler 审批分配，Dispatch 执行、超时、效果及 POST。工具以 execute(arguments, *, context) 接收可信上下文。

默认 calculator、read_file、write_file、edit_file、glob、grep、bash、use_skill、skill_manage，Memory 增加 memory_search/read/edit。本地工具按绝对路径访问本机，不设工作区沙箱。Action 保留授权、预算、版本、幂等、恢复与计划。

Action 实现在 agent/core/action，由 Runtime 装配；ToolHandler / ToolDispatch 仍是统一审批与执行入口，共享类型保留在 agent/types/action。

Registry 只暴露 active、scope 可见、满足 required_tools 的技能。skills/selector.py 选择相关技能，选择和正文整轮冻结。

Repository 是写入口，保留内置只读、原子写、版本、备份、冲突、scope 和审计。skill_manage 创建 draft 或修改正文，可信入口审查启用，Runtime 不自动晋升。

Skill Evolution 和候选后台回放已删除。通用评测、比较、消融和 Memory 评测保留。当前没有 Plugin 加载器。
