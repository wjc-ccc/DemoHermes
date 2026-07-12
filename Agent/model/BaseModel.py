"""
BaseModel — 模型抽象基类

所有 LLM Provider 必须实现此接口，对上层（core/loop）隐藏具体 API 差异：
    chat(messages: list[dict]) -> str

messages 格式遵循 OpenAI 标准：[{"role": "user", "content": "..."}]
"""
from abc import ABC, abstractmethod


class BaseModel(ABC):

    @abstractmethod
    def chat(self, messages: list[dict]) -> str:
        """发送消息列表，返回模型回复文本。"""
        pass
