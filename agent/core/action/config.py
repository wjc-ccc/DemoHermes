"""Action 的设置入口。

默认值在 ActionConfig 上。启动时用 RuntimeConfig.action_config 覆盖；
不传则使用这些默认值。工具各自的风险、超时和每轮调用上限在工具的
action_policy 上，不在这里。
"""
from agent.types.action.config import ActionConfig

__all__ = ["ActionConfig"]
