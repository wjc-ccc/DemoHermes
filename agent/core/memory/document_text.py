"""Markdown token 估算的纯函数。"""
def estimate_markdown_tokens(content: str) -> int:
    """中英文混排按 UTF-8 字节保守估算。"""
    return max(len(content.split()), (len(content.encode("utf-8")) + 2) // 3) if content else 0
