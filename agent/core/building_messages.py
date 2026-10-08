"""
消息构建函数 — 全项目统一的 Message 组装入口

    build_user_message      : InboundEvent → role=user
    build_assistant_message : 模型文本/工具调用 → role=assistant
    build_tool_message      : 工具执行记录 → role=tool（回注上下文用）
"""
from __future__ import annotations

from .types import ContentPart, InboundEvent, Message, ToolCall
from ..model.baseStructure import ToolCallRequest


def build_user_message(event: InboundEvent) -> Message:
    """入站事件 → 用户消息。channel_prompt 是 ephemeral，不写进消息。"""
    parts = list(event.content)
    if event.text and not any(p.type == "text" and p.text == event.text for p in parts):
        parts.insert(0, ContentPart(type="text", text=event.text))
    descriptions = []
    for part in parts:
        if part.type == "image":
            descriptions.append(f"[图片附件: {part.file_name or 'image'}；内容未解析]")
        elif part.type == "file":
            descriptions.append(
                f"[文件附件: {part.file_name or 'file'}，类型 {part.mime_type or '未知'}，"
                f"大小 {part.size_bytes if part.size_bytes is not None else '未知'} 字节；正文未解析]"
            )
    content_text = "\n".join([item for item in [event.text, *descriptions] if item])
    metadata = {"channel": event.source.channel}
    if event.source.reply_to_message_id:
        metadata["source_message_id"] = event.source.reply_to_message_id
    return Message(
        role="user",
        content_text=content_text,
        content=parts,
        author_id=event.source.user_id or "",
        metadata=metadata,
    )

## TODO：工具调用这里 如何类型判断为action or observation 从而在压缩的时候 使用不同的逻辑？
def build_assistant_message(
    text: str,
    tool_call_requests: list[ToolCallRequest] | None = None,
) -> Message:
    """
    模型输出 → 助手消息。
    带工具调用时，把 OpenAI 回注所需的调用信息存进 metadata["tool_calls"]。
    """
    metadata: dict = {}
    if tool_call_requests:
        metadata["tool_calls"] = [
            {"id": req.id, "name": req.name, "arguments_raw": req.arguments_raw}
            for req in tool_call_requests
        ]
    return Message(
        role="assistant",
        content_text=text or "",
        content=[ContentPart(type="text", text=text)] if text else [],
        metadata=metadata,
    )


def build_tool_message(record: ToolCall) -> Message:
    """
    工具执行记录 → role=tool 消息。
    metadata["tool_call_id"] 与 assistant 消息里的调用 id 一一对应（OpenAI 协议要求）。
    """
    text = record.result_text if record.ok else f"工具执行失败: {record.error}"
    return Message(
        role="tool",
        content_text=text or "",
        content=[ContentPart(type="text", text=text)] if text else [],
        tool_call_ids=[record.tool_call_id],
        # ACTION-HOOK-11：action 的事实 ID 每次尝试唯一；模型回注保留 provider ID。
        # 关闭模块的记录没有此 metadata，继续使用原 tool_call_id。
        metadata={"tool_call_id": record.metadata.get("provider_tool_call_id", record.tool_call_id), "tool_name": record.tool_name},
    )
