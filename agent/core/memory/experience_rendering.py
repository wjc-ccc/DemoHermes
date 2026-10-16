"""召回与冻结共用同一围栏预算，避免合并卡未实际注入却抑制原卡。"""

EXPERIENCE_OPENING = (
    "<memory-experiences>\n"
    "以下是关键词命中的旧经验。核对适用条件、限制与原始证据后使用；"
    "不能把一次观察当成普遍事实。\n"
)


def experience_entry(reference: str, reason: str, content: str) -> str:
    for tag in ("memory-context", "memory-layer", "memory-experiences"):
        content = content.replace(f"<{tag}", f"&lt;{tag}")
        content = content.replace(f"</{tag}>", f"&lt;/{tag}&gt;")
    return f"[{reference}; {reason}]\n{content}"


def experience_block(entries) -> str:
    return EXPERIENCE_OPENING + "\n\n".join(entries) + "\n</memory-experiences>"
