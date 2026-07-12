"""
ContextBuilder — LLM 上下文组装器

将多层信息按优先级拼成发给 LLM 的 messages 列表：
    1. System Prompt（由 prompt/builder 分层构建）
    2. Hot Memory 快照（会话开始时冻结）
    3. Episodic 检索结果（按当前问题召回相关历史）
    4. 匹配的 Skill 内容（渐进加载）
    5. 当前对话消息（user / assistant / tool）

控制 token 用量，避免上下文溢出。
"""
