"""MemoryProvider — 外部记忆系统唯一旁路 seam。

只暴露两个方法：
    recall(request)     组装上下文时按 query 取回相关文本
    sync_turn(turn, session)  完成 turn 后把完整对话交给外部（黑盒自存）

不处理外部记忆的增删改查；主链路只认 builtin Hot/Daily。
recall 失败/超时由 Manager 降级；sync_turn 不阻断回复。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.types import Session
from .types import ProviderContribution, ProviderRecallRequest, TurnRecord

### 输入完整TurnRecord和Session来同步  +  输入Provider的召回请求类 然后 得到召回响应类
class MemoryProvider(ABC):
    """外部记忆 Provider：旁路 recall + sync_turn，存取实现自管。"""

    name: str

    @abstractmethod
    def recall(self, request: ProviderRecallRequest) -> ProviderContribution:
        """针对当前 query 返回相关上下文贡献；失败/超时由 Manager 捕获降级。"""

    def sync_turn(self, turn: TurnRecord, session: Session) -> None:
        """完成 turn 后接收完整对话（turn 信封 + Session.messages）；默认 no-op。"""
        return None
