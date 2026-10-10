"""
Types - 核心数据结构

定义 Agent 各模块共用的数据类型：
    - Message      : 单条对话消息（message_id,session_id,role, content, tool_calls）
    - ToolCall     : 一次工具调用的执行记录
    - Anchor       : 与 Session 绑定的历史压缩状态和覆盖账本
    - AdmissionVerdict : 历史工具结果的上下文准入定案（keep / trim / defer）
    - Session : 会话（id, messages, tool_calls, created_at）
    - InboundEvent : 入站事件（text, source, content, channel_prompt, metadata）
    - OutboundReply : 出站回复（text, source, metadata）
    - TurnResult : 一轮对话结果（final_text, session_id, completed, interrupted, error）

sessions 会话容器 「这次聊天」的元信息与生命周期
    -- messages 对话事实  发给 LLM / 展示给用户的消息流
    -- tool_calls 工具事实 一次工具调用的请求、执行、结果

"""
from __future__ import annotations
import time
import uuid
from typing import Any, Dict, Literal
from pydantic import BaseModel, Field, ValidationError, field_validator

from agent.types.context import Anchor, AdmissionVerdict
from agent.types.action.execution import ActionExecutionEvidence
from agent.types.action.trace import ActionTrace
from agent.types.memory.mutation import MemoryMutationItem


#**** Message ****
class ContentPart(BaseModel):  ## 包含字段较为全面  7.13 ✅️
    ## content part basic info -- multimodal ability
    type:Literal["text","image","audio","video","file"] = Field(default="text",description="content type")
    ## text
    text:str = Field(default_factory=str,description="content text")
    ## file
    path:str|None = Field(default=None,description="file path")
    file_name:str|None = Field(default=None,description="attachment file name")
    mime_type:str|None = Field(default=None,description="attachment MIME type")
    size_bytes:int|None = Field(default=None,description="attachment size")
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
    provider_tool_call_id: str | None = None
    pending_mutation: MemoryMutationItem | None = None
    action_evidence: ActionExecutionEvidence | None = None
    action_trace: ActionTrace | None = None
    readback_reference: str | None = None

    def observation_payload(self) -> dict:
        """传输层投影：保持现有 HTTP/Debug 的工具事实字段。"""
        value = self.model_dump(mode="json", exclude={
            "provider_tool_call_id", "pending_mutation", "action_evidence", "action_trace", "readback_reference",
        })
        if self.provider_tool_call_id is not None:
            value["metadata"]["provider_tool_call_id"] = self.provider_tool_call_id
        if self.action_evidence is not None:
            value["metadata"]["action"] = self.action_evidence.model_dump(mode="json")
        if self.action_trace is not None:
            value["metadata"]["action_trace"] = self.action_trace.model_dump(mode="json")
        if self.pending_mutation is not None:
            value["tool_call_result"] = {
                **(value["tool_call_result"] or {}),
                "memory_mutation": self.pending_mutation.command_payload(),
            }
        return value

    @property
    def ok(self) -> bool:
        """是否执行成功（由 status 推导，不单独存储）。"""
        return self.status == "success"


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
    ### 每个session的message对应的准入定案 这个是上下文准入的逻辑（每个context item是否进入以及进入凭证是什么） 
    context_verdicts: dict[str, AdmissionVerdict] = Field(
        default_factory=dict,
        description="上下文准入定案，按 message_id 索引；只影响发给模型的投影，不改 messages",
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

    @field_validator("context_verdicts", mode="before")
    @classmethod
    def _drop_invalid_verdicts(cls, value):
        """损坏的定案按未判处理，不阻断 Session 恢复。"""
        if not isinstance(value, dict):
            return {}
        restored = {}
        for key, verdict in value.items():
            try:
                restored[key] = (
                    verdict
                    if isinstance(verdict, AdmissionVerdict)
                    else AdmissionVerdict.model_validate(verdict)
                )
            except (TypeError, ValueError, ValidationError):
                continue
        return restored

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


#*** 单个 turn信息统计 ***
class TurnResult(BaseModel):
    """Loop → Gateway：一轮对话结果。"""
    final_text: str | None = None
    session_id: str = Field(default_factory=str)
    turn_id: str = Field(default_factory=str)
    completed: bool = True
    interrupted: bool = False
    error: str | None = None


class SessionReset(BaseModel):
    closed_session: Session | None
    new_session: Session
