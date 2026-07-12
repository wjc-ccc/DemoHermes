"""
EvolutionScheduler — 进化任务调度

管理进化流程的触发时机：
    - 回合结束后立即触发 reviewer（同步）
    - 定时触发 memory_curator 整理（异步 / cron）
    - 批量 skill_optimizer 优化（低优先级后台）

避免进化任务阻塞主对话循环。
"""
