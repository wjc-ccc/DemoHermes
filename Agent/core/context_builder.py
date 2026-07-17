"""
ContextBuilder - LLM 上下文组装器

将多层信息按优先级拼成发给 LLM 的 messages 列表：
    1. System Prompt（由 prompt/builder 分层构建）
    2. Hot Memory 快照（会话开始时冻结）
    3. Episodic 检索结果（按当前问题召回相关历史）
    4. 匹配的 Skill 内容（渐进加载）
    5. 当前对话消息（user / assistant / tool）

控制 token 用量，避免上下文溢出。
"""
from .types import Session
from ..provider import SYSTEM_PROMPT_PATH

"""
1.统计上下文长度
2.加载skill tool
3.Hot Memor加载
4.加载message 返回最终prompt
5.保存数据库当前上下文具体内容，记录长度，时间，

"""

class ContextBuilder:

    def __init__(self, session: Session):
        self.session: Session = session
        self.messages: list = []  # 组装后发给 ai 的 messages

    def _loading_system_prompt(self):
        # 从 provider 配置的路径加载 systemPrompt.md
        self.messages.append({
            "role": "system",
            "content": SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        })

    def _freezing_snapshot(self):
        pass

    def _loading_hot_memory(self):
        pass

    def _loading_skills(self):
        pass

    def _loading_tools(self):
        pass

    def _loading_messages(self):
        for m in self.session.messages:
            self.messages.append({"role": m.role, "content": m.content_text})

    def _backing_length(self):
        return len(self.messages)

    def building_context(self) -> list:
        self.messages = []  # 每轮重置
        self._loading_system_prompt()
        self._loading_hot_memory()
        self._loading_skills()
        self._loading_tools()
        self._loading_messages()
        return self.messages


class ContextSaver:

    def saving(self):
        pass
