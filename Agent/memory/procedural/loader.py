"""
ProceduralLoader — L3 Skill 三级渐进加载

按需展开 Skill 内容，节省 token：
    Level 1 — name + 一句话描述（始终在 prompt 中）
    Level 2 — 参数说明（LLM 决定使用时加载）
    Level 3 — 完整执行步骤（实际调用时加载）

类似 Cursor Skills 的加载策略。
"""
