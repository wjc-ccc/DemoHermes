# Tool、Skill 与 Plugin 的当前状态

代码入口：`agent/tools/`、`agent/skills/`、`agent/evolution/`；实际启用项以 `agent/main.py:build_gateway` 的注册为准。

## Tool：执行动作

`ToolRegistry` 收集工具 schema，并用 `dispatch(name, arguments) -> ToolCallResult` 分发。Loop 根据模型的 `tool_calls` 执行，将结果记为 `ToolCall` 和 `role=tool` 消息，下一次模型调用能看到执行结果。

| 默认装配 | 条件 | 用途 |
| --- | --- | --- |
| `CalculatorTool` | 始终 | 计算 |
| `UseSkillTool` | 始终 | 按选中的 Skill 快照加载正文 |
| `SkillManageTool` | 始终 | 经 Skill Repository 管理技能 |
| `MemorySearchTool`、`MemoryReadTool`、`MemoryEditTool` | `use_memory=True` | 检索、读取和显式编辑记忆 |

`agent/tools/bash_tools.py`、`file_tools.py` 里有实现文件，但当前 `build_gateway` 没有注册它们。不能因为源码存在就认为模型默认可调用。记忆工具的 `_scope` 由 Loop 在分发时注入，不接受模型自选用户或群聊归属。

## Skill：提供可复用经验

Skill 的内容存于 `SKILL.md`，由 `SkillRegistry` 和 `SkillRepository` 管理。模型先看按查询筛出的技能摘要，需要流程正文时调用 `use_skill`。Skill 文本本身不执行程序；能做的操作仍由已注册的 Tool 决定。

内置示例位于 `agent/skills/builtin/weekly_report/SKILL.md`；运行期生成的 Skill 位于 `data/skills/`。`agent/evolution/` 可依据已提交轨迹提出草案或补丁，Repository 管理修订、状态和审计。`SkillLifecycleGate` 通过隔离回放结果控制晋升；当前生产装配没有给 `SkillEvolution` 提供 `runner_factory`，新草案不会自动进入 active。

## Plugin：目前没有运行时模块

仓库没有已接线的 `plugin/` 加载器或清单协议。ToolRegistry 和 SkillRegistry 可作为以后设计扩展点的参考，但“Plugin”目前只是概念，不应写成现有可安装或热插拔能力。

## 修改入口

新增执行能力：实现 Tool Interface，在 `build_gateway` 注册，并检查模型 schema 与结果回注。新增经验流程：编写 Skill、确认选择和 `use_skill` 正文加载。新增自动进化规则：修改 Evolution 的提案/回放链，而不是把生成文本直接塞进 Tool 或 Loop。
