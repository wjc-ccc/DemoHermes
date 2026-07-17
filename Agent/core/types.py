"""
Types - 核心数据结构

定义 Agent 各模块共用的数据类型：
    - Message      : 单条对话消息（message_id,session_id,role, content, tool_calls）
    - Tool     : 工具调用请求（name, arguments, id）
    - MemoryItem   : 记忆条目（layer, content, metadata, timestamp）
    - Session : 会话（id, messages, hot_snapshot, created_at）
    - InboundEvent : 入站事件（text, source, content, channel_prompt, metadata）
    - OutboundReply : 出站回复（text, reply_to, metadata）
    - TurnResult : 一轮对话结果（final_text, session_id, completed, interrupted, error）


使用 pydantic 实现，保证类型安全。Field default_factory,choices,description

sessions 会话容器 「这次聊天」的元信息与生命周期
    -- messages 对话事实  发给 LLM / 展示给用户的消息流
    -- tool_calls 工具事实 一次工具调用的请求、执行、结果
    -- memories 知识沉淀  从对话中提炼的可检索知识（L1/L2/L3）
    -- trajectories 执行轨迹  Agent 怎么做的（可回放、可进化、可观测）

"""
from random import choice
from typing import Tuple, Optional, List, Dict, Any, Literal
import uuid
import time
from pydantic import BaseModel, Field


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


#**** Memory ****
class MemoryItem(BaseModel):
    ## memory basic info
    memory_id:str = Field(default_factory=lambda: str(uuid.uuid4()))


    ## memory items
    created_at:str = Field(default_factory=str,description="memory created time")
    metadata:dict = Field(default_factory=dict,description="memory metadata")

#**** Session ****
class Session(BaseModel): ## 包含字段较为全面  7.13 ✅️  提供add message接口 7.14✅️
    """
    Session
    ├── SessionInfo
    ├── TimeInfo
    ├── ToolCallInfo
    └── MemoryInfo
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

    ## memory info
    memory:list[MemoryItem] = Field(default_factory=list) ## 记忆数据 用于针对session进行总结更新
    summary:str = Field(default_factory=str,description="session summary")## 会话摘要 用于后续的更新

    ## 运行期路由键（不进 LLM；由 Gateway 写入）
    session_key: str = Field(default_factory=str, description="逻辑会话槽 agent:...")

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


class InboundEvent(BaseModel):
    """Channel → Gateway 唯一入站。平台字段已在 Channel 内映射完成。"""
    text: str = Field(default_factory=str)
    source: SessionSource = Field(default_factory=SessionSource)
    channel_prompt: str | None = Field(
        default=None,
        description="仅当次注入 API 的平台说明，不写进 Session.messages",
    )
    metadata: dict = Field(default_factory=dict)


class OutboundReply(BaseModel):
    """Gateway → Channel 出站。只含发送所需；内部状态留在 Gateway。"""
    text: str = Field(default_factory=str)
    reply_to: str | None = None
    # 出站路由：dispatcher 靠 source.channel 选 Channel.deliver_to_OutboundEvent
    source: SessionSource = Field(default_factory=SessionSource)
    metadata: dict = Field(default_factory=dict)


class TurnResult(BaseModel):
    """Loop → Gateway：一轮对话结果。"""
    final_text: str | None = None
    session_id: str = Field(default_factory=str)
    completed: bool = True
    interrupted: bool = False
    error: str | None = None
