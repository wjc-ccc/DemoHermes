"""
PromptSnapshot — Hot Memory 快照冻结

在 Session 创建时，将 USER.md 和 MEMORY.md 的内容冻结为不可变快照：
    - 保证同一会话内 prompt 稳定，不会因中途写入而变化
    - 新会话开始时重新读取最新内容

由 core/session.py 在创建会话时调用。
"""
