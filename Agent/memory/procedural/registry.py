"""
ProceduralRegistry — L3 Skill 注册表

管理所有可用 Skill 的注册与查找：
    - register(skill)  : 注册新 Skill
    - list_all()       : 列出所有 Skill 摘要（Level 1）
    - get(name)        : 按名称获取 Skill
    - search(query)    : 按关键词搜索 Skill

启动时扫描 skills/ 和 data/skills/ 自动注册。
"""
