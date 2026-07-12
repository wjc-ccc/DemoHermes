"""
HotStore — L1 热记忆读写

管理两个 Markdown 文件：
    - data/memories/USER.md   : 用户画像（偏好、背景、习惯）
    - data/memories/MEMORY.md : Agent 长期笔记（项目上下文、重要结论）

这两个文件在每轮对话中始终作为 System Prompt 的一部分注入。
"""
