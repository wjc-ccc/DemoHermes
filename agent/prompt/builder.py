"""
PromptBuilder — 分层 System Prompt 构建器

按优先级组装 System Prompt 各层：
    1. identity  — 从 templates/identity.md 加载 Agent 身份与行为准则
    2. tools     — 从 templates/tools_guide.md + 工具 schema 生成使用指引
    3. memory    — 注入 Hot 快照 + 检索到的 Episodic 片段 + Skill 摘要
    4. volatile  — 当前任务上下文、临时指令（每轮可变）

最终输出完整的 system message 字符串。
"""
