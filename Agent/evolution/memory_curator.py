"""
MemoryCurator — 热记忆自动整理

定期或在 reviewer 触发时，压缩整理 USER.md 和 MEMORY.md：
    - 合并重复信息
    - 删除过时内容
    - 控制在字符上限内

调用 memory/hot/curator.py 执行具体整理逻辑。
"""
