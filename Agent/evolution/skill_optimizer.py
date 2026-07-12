"""
SkillOptimizer — Skill 自我修正

在 Skill 执行过程中或执行后：
    - 检测执行失败或低效步骤
    - 用 LLM 分析原因并修正 SKILL.md
    - 更新 procedural/store 中的 Skill 文件

实现 Skill 的"越用越好"。
"""
