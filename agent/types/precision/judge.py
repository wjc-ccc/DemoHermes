"""precision / judge 数据契约。"""
from __future__ import annotations
from typing import Protocol, Sequence
from pydantic import BaseModel, ConfigDict


class ContextJudgeRequest(BaseModel):
    """对一条历史工具结果的一次判官提问。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    goal: str
    call: str
    result_head: str
    result_chars: int
    age_turns: int
    calls_since: int


class ContextJudgeResult(BaseModel):
    """判官对一条请求的回答；字段为 None 表示该题没有可信答案。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    kind: str | None = None
    still_needed: float | None = None


class ContextJudge(Protocol):
    """准入层依赖的唯一判官接口；结果顺序必须与请求一致。"""

    id: str

    def judge(
        self, requests: Sequence[ContextJudgeRequest]
    ) -> list[ContextJudgeResult]:
        """批量回答；整体失败时抛 ContextJudgeError。"""
        ...
