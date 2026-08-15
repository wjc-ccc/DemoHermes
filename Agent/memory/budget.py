"""
Markdown 文件字符数限制 apply后判断是否需要summarize。

这些是字符数，不是模型 tokenizer 的精确 token 数；整轮 prompt 的 token 预算走core/context_budget.py。

Daily 不默认进 prompt，不做破坏性总量截断，故无总量上限。
"""

PROFILE_MAX_CHARS = 1375  # USER.md / GROUP.md
MEMORY_MAX_CHARS = 2200  # MEMORY.md

# target -> 字符硬上限；daily 无总量上限
HOT_MAX_CHARS = {"profile": PROFILE_MAX_CHARS, "memory": MEMORY_MAX_CHARS}
