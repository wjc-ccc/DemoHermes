"""
Types - 核心数据结构

定义 Agent 各模块共用的数据类型：
    - Message      : 单条对话消息（message_id,session_id,role, content, tool_calls）
    - ToolCall     : 一次工具调用的执行记录
    - Anchor       : 与 Session 绑定的历史压缩状态和覆盖账本
    - Session : 会话（id, messages, tool_calls, created_at）
    - InboundEvent : 入站事件（text, source, content, channel_prompt, metadata）
    - OutboundReply : 出站回复（text, reply_to, metadata）
    - TurnResult : 一轮对话结果（final_text, session_id, completed, interrupted, error）

sessions 会话容器 「这次聊天」的元信息与生命周期
    -- messages 对话事实  发给 LLM / 展示给用户的消息流
    -- tool_calls 工具事实 一次工具调用的请求、执行、结果

"""
from typing import  Dict, Any, Literal
import uuid
import time
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


#**** Message ****
class ContentPart(BaseModel):  ## 包含字段较为全面  7.13 ✅️
    ## content part basic info -- multimodal ability
    type:Literal["text","image","audio","video","file"] = Field(default="text",description="content type")
    ## text
    text:str = Field(default_factory=str,description="content text")
    ## file
    path:str|None = Field(default=None,description="file path")
    ## image
    url:str|None = Field(default=None,description="image audio or video path")
    ## other arguments
    data:Dict[str,Any]|None = Field(default=None,description="content data")

class Message(BaseModel):  ## 包含字段较为全面  7.13 ✅️
    """
    Message
    ├── BasicInfo
    ├── TimeInfo
    ├── ToolCallInfo
    └── MemoryInfo
    """
    ## message basic info
    message_id:str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id:str = Field(default_factory=str,description="session id")
    trajectory_id:str|None = Field(default=None) # which trajectory

    ## message info
    role:Literal["user","assistant","system","tool"] = Field(default="user",description="message from whom")
    author_id:str = Field(default_factory=str,description="message author id")
    seq_id:int = Field(default_factory=int,description="message index in session")
    status:Literal["completed","failed"] = Field(default="completed",description="whether message completed or not")

    ## message content
    content:list[ContentPart] = Field(default_factory=list,description="message content")
    content_text:str = Field(default_factory=str,description="only text info")

    ## time info
    created_at:float = Field(default_factory=time.time,description="message created time")

    ## tool call info
    tool_call_ids:list = Field(default_factory=list,description="message tool call id")

    metadata:dict = Field(default_factory=dict,description="message metadata")

#**** Tool ****
class ToolCall(BaseModel): ## 包含字段较为全面  7.13 ✅️
    ## toolcall basic info
    tool_call_id:str = Field(default_factory=lambda: str(uuid.uuid4()),description="tool call id")
    session_id:str = Field(default_factory=str)
    trajectory_id:str = Field(default_factory=str)

    ## toolcall info
    tool_name:str = Field(default_factory=str,description="tool name")
    arguments:dict = Field(default_factory=dict,description="tool arguments")
    arguments_raw:str = Field(default_factory=str) ## 调试用的参数 后续删除
    status:Literal["pending","running","success","error","timeout","cancelled"] = Field(default="pending")

    ## time info
    created_at:float = Field(default_factory=time.time,description="toolcall created time")
    finished_at:float = Field(default_factory=time.time,description="toolcall finished time")
    durations_ms:int = Field(default_factory=int)

    ## toolcall result
    tool_call_result:dict|None = Field(default=None)  # 结构化结果
    result_text:str|None = Field(default=None)  # 文本结果（回注 LLM 用）
    error:str|None = Field(default=None)
    metadata:dict = Field(default_factory=dict,description="tool call metadata")

    @property
    def ok(self) -> bool:
        """是否执行成功（由 status 推导，不单独存储）。"""
        return self.status == "success"


# **** Anchor ****
class AnchorFact(BaseModel):
    """Anchor 中一条可追溯的语义事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    source_message_ids: tuple[str, ...] = ()
    status: Literal["active", "completed", "failed"] = "active"


class Anchor(BaseModel):
    """Session 历史的版本化压缩结果和连续覆盖账本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = 2
    anchor_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    covered_start_seq: int = Field(ge=0)
    covered_end_seq: int = Field(ge=0)
    covered_start_message_id: str = Field(min_length=1)
    covered_end_message_id: str = Field(min_length=1)
    covered_turn_count: int = Field(ge=1)
    source_digest: str = Field(min_length=1)
    intent: tuple[AnchorFact, ...] = ()
    changes: tuple[AnchorFact, ...] = ()
    decisions: tuple[AnchorFact, ...] = ()
    next_steps: tuple[AnchorFact, ...] = ()
    excluded: tuple[AnchorFact, ...] = ()

#**** Session ****
class Session(BaseModel): ## 包含字段较为全面  7.13 ✅️  提供add message接口 7.14✅️
    """
    Session
    ├── SessionInfo
    ├── TimeInfo
    └── ToolCallInfo
    """
    ## session basic info
    session_id:str = Field(default_factory=lambda: str(uuid.uuid4()))
    author_id:str = Field(default_factory=str)
    status:Literal["active","paused","completed"] = Field(default="active") ## 会话状态
    channel:str = Field(default_factory=str,description="channel type")

    ## time info
    created_at:float = Field(default_factory=time.time)
    updated_at:float = Field(default_factory=time.time) ## 更新时间
    last_active_at:float = Field(default_factory=time.time) ## 最近启动时间

    ## message info
    messages:list[Message] = Field(default_factory=list)
    message_count:int = Field(default_factory=int,description="session message count")


    ## tool_call info
    tool_calls:list[ToolCall] = Field(default_factory=list)

    summary:str = Field(default_factory=str,description="legacy session summary")## 仅用于旧 Anchor 迁移
    context_anchor:Anchor|dict|None = Field(
        default=None,
        description="Precision v2 结构化 Anchor；覆盖账本由 PrecisionBuilder 校验",
    )

    ## 运行期路由键（不进 LLM；由 Gateway 写入）
    session_key: str = Field(default_factory=str, description="逻辑会话槽 agent:...")

    @field_validator("context_anchor", mode="before")
    @classmethod
    def _restore_valid_anchor(cls, value):
        """恢复合法旧字典为 Anchor；损坏载荷保留给 Precision 降级。"""
        if isinstance(value, Anchor) or not isinstance(value, dict):
            return value
        try:
            return Anchor.model_validate(value)
        except (TypeError, ValueError, ValidationError):
            return value

    def add_message(self, msg: "Message") -> "Message":
        """把消息追加进本会话，维护 seq / 计数 / 时间戳。"""
        msg.session_id = self.session_id
        msg.seq_id = len(self.messages)
        self.messages.append(msg)
        self.message_count = len(self.messages)
        now = time.time()
        self.updated_at = now
        self.last_active_at = now
        return msg

#**** Channel ****
class SessionSource(BaseModel):
    """消息从哪来、回哪去。"""
    channel: str = Field(default="cli", description="cli / wechat / feishu / api ...")
    chat_id: str = Field(default="local")
    chat_type: Literal["dm", "group", "thread"] = "dm"
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    reply_to_message_id: str | None = None
    metadata: dict = Field(default_factory=dict)

#*** message bus 输入 ***
class InboundEvent(BaseModel):
    """Channel → Gateway 唯一入站。平台字段已在 Channel 内映射完成。"""
    text: str = Field(default_factory=str)
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
    reply_to: str | None = None
    # 出站路由：dispatcher 靠 source.channel 选 Channel.deliver_to_OutboundEvent
    source: SessionSource = Field(default_factory=SessionSource)
    metadata: dict = Field(default_factory=dict)

#*** 单个 turn信息统计 ***
class TurnResult(BaseModel):
    """Loop → Gateway：一轮对话结果。"""
    final_text: str | None = None
    session_id: str = Field(default_factory=str)
    turn_id: str = Field(default_factory=str)
    completed: bool = True
    interrupted: bool = False
    error: str | None = None
