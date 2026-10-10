"""channel 数据契约。"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

from agent.types.conversation import ContentPart


#**** Channel ****
class SessionSource(BaseModel):
    """消息从哪来、回哪去。"""
    channel: str = Field(default="cli", description="cli / wechat / feishu / api ...")
    chat_id: str = Field(default="local")
    chat_type: Literal["dm", "group", "thread"] = "dm"
    user_id: str | None = None
    thread_id: str | None = None
    reply_to_message_id: str | None = None
    metadata: dict = Field(default_factory=dict)


#*** message bus 输入 ***
class InboundEvent(BaseModel):
    """Channel → Gateway 唯一入站。平台字段已在 Channel 内映射完成。"""
    text: str = Field(default_factory=str)
    content: list[ContentPart] = Field(default_factory=list)
    source: SessionSource = Field(default_factory=SessionSource)
    channel_prompt: str | None = Field(
        default=None,
        description="仅当次注入 API 的平台说明，不写进 Session.messages",
    )
    metadata: dict = Field(default_factory=dict)


#*** message bus 输出 ***
class OutboundReply(BaseModel):
    """Gateway → Channel 出站。只含发送所需；内部状态留在 Gateway。"""
    text: str = Field(default_factory=str)
    # 出站路由：dispatcher 靠 source.channel 选 Channel.deliver_to_OutboundEvent；
    # 回复哪条平台消息看 source.reply_to_message_id
    source: SessionSource = Field(default_factory=SessionSource)
    metadata: dict = Field(default_factory=dict)
