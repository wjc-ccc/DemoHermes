"""
MemoryManager — 记忆编排中枢

统一调度三层记忆的读写，对上层（core/loop, tools/memory_tools）暴露简洁 API：
    - get_hot_snapshot()     : 获取 L1 热记忆快照
    - record_turn(turn)      : 将一轮对话写入 L2 情景记忆
    - search_episodic(query) : 检索历史对话片段
    - load_skill(name)       : 渐进加载 L3 Skill
    - save_skill(skill)      : 持久化新 Skill

是 memory 包对外的唯一入口。
"""
