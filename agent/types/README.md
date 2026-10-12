# 公开数据契约

agent/types/ 是共享结构、类别、结果和 Protocol 的唯一位置。直接导入具体文件，独立导入全部类型不加载 core、Gateway、Store、Worker 或 SDK。

| 领域 | 内容 |
| --- | --- |
| runtime.py | RuntimeConfig、LoopConfig、TurnContext、RuntimeDescription、EnvironmentSnapshot |
| tools.py | ToolCallResult、ToolInvocationContext、ToolTurnContext、ToolDispatchItem、ToolDefinition |
| conversation.py / channel.py | 对话事实与渠道输入输出 |
| context.py / perception/ | Anchor、预算、准入、结果、指标、Trace、Priority |
| memory/ | scope、层级、类别、卡片、快照、提炼、修改、读取、协议及 Trace |
| action/ | 配置、AssemblyPolicy、ToolContext、执行证据、计划、链段和协议 |
| storage.py | Job、归档、AuditEntry、提交回执 |
| skills.py | Skill、冻结选择、草案及可信变更 |
| eval/ | 案例、报告、比较、消融与数据集 |

字段单位和约束维护在定义旁，纯身份、校验与渲染可随数据；IO 与流程留在实现。内部 ContextItem 和 Perception 阶段状态留在 core/perception。类别仅在跨模块共享时进入 types。

直接修改调用方，不提供旧路径包装。Skill 解析在 agent.skills.base，MemoryScope 构建在 core/memory/memory_scope.py。前端 types 表达传输与展示数据。
