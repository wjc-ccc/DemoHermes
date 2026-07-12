"""
HotCurator — L1 热记忆整理器

防止 USER.md / MEMORY.md 无限膨胀：
    - 字符上限控制（超出时触发压缩）
    - 内容合并与去重
    - 过期信息清理

由 evolution/memory_curator.py 定期调用，也可在写入时实时检查。
"""
