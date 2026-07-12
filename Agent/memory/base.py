"""
MemoryStore / MemoryProvider — 记忆层抽象基类

定义所有记忆层必须实现的统一接口：
    - read(key)        : 读取记忆
    - write(key, data) : 写入记忆
    - search(query)    : 检索记忆
    - delete(key)      : 删除记忆

Hot / Episodic / Procedural 各自继承此基类，保证接口一致。
"""
