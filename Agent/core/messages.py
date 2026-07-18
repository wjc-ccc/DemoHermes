"""
消息构建函数 — 全项目统一的 Message 组装入口

三种消息的构建规则集中在这里，避免 Gateway / Loop 各自手拼字段：
    build_user_message      : InboundEvent → role=user
    build_assistant_message : 模型文本/工具调用 → role=assistant
    build_tool_message      : 工具执行记录 → role=tool（回注上下文用）

OpenAI 协议需要的 tool_calls / tool_call_id 字段放在 Message.metadata 里，
由 ContextBuilder 在组装时映射成 API 格式 —— 数据结构本身不绑定任何 provider。
"""
from __future__ import annotations

from .types import ContentPart, InboundEvent, Message, ToolCall
from ..model.baseStructure import ToolCallRequest


def build_user_message(event: InboundEvent) -> Message:
    """入站事件 → 用户消息。channel_prompt 是 ephemeral，不写进消息。"""
    return Message(
        role="user",
        content_text=event.text,
        content=[ContentPart(type="text", text=event.text)] if event.text else [],
        author_id=event.source.user_id or "",
        metadata={"channel": event.source.channel},
    )


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
        metadata={"tool_call_id": record.tool_call_id, "tool_name": record.tool_name},
    )
