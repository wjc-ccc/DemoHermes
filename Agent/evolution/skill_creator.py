"""
SkillCreator — 自动 Skill 创建

当 reviewer 发现 Agent 完成了一个复杂但可复用的任务流程时：
    - 从对话轨迹中提取关键步骤
    - 生成 SKILL.md 文件
    - 注册到 memory/procedural/registry

新 Skill 存放到 data/skills/ 目录。
"""
