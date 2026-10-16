"""
Agent/cron 包 — 定时任务与邮箱唤醒

    jobs.py          按用户存放的任务文件（JobStore）
    schedule_tool.py schedule 工具：登记 / 查看 / 删除当前用户的任务
    scheduler.py     后台线程：到点后用原会话来源提交一轮新会话
    mail.py          网易邮箱 IMAP 增量拉取
"""
