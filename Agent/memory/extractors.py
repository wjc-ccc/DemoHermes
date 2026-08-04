"""MemoryExtractor 实现 — 规则化抽取（7.23 方案 §8.2）。

方案允许本阶段用规则/禁用模型；真实 LLM 抽取可后续替换同一 seam。
Reviewer 只消费 Extractor 产出的 MemoryMutation，经 Manager.apply 落库：
  - 稳定偏好 → profile（USER.md / GROUP.md）
  - 对 Agent 的长期约束 → memory（MEMORY.md）
  - 日常事件 → daily（daily/YYYY-MM-DD.md）
  - 一次性问题 / 招呼 → 不写
"""
from __future__ import annotations

import logging

from .types import BuiltinMemoryMutation, MemoryProvenance, TurnEnvelope

logger = logging.getLogger(__name__)

_PREF_MARKERS = (
    "偏好", "喜欢", "请记住", "记住我", "以后请", "以后都", "总是",
    "不要再", "我叫", "我的名字", "习惯",
)
_AGENT_MARKERS = (
    "你必须", "请始终", "你是", "记住你", "作为助手", "回答时请",
)
_GREETINGS = {"你好", "您好", "hi", "hello", "hey", "在吗", "早上好", "晚上好"}


class RuleBasedExtractor:
    """从 TurnEnvelope 对应的用户消息做启发式抽取。

    需要能按 session_id 加载 Session 的 store（SqliteSessionStore.get_session）。
    """

    def __init__(self, store) -> None:
        self._store = store

    def extract(self, turn: TurnEnvelope) -> list[BuiltinMemoryMutation]:
        if turn.interrupted or not turn.completed:
            return []

        session = self._store.get_session(turn.session_id)
        if session is None:
            logger.warning("review extract: session 不存在 id=%s", turn.session_id)
            return []

        user = next(
            (m for m in session.messages if m.message_id == turn.user_message_id),
            None,
        )
        if user is None:
            return []

        text = (user.content_text or "").strip()
        if not text or text.startswith("/"):
            return []
        if text.casefold() in _GREETINGS or len(text) < 4:
            return []

        # 一次性提问：不问偏好/约束就不写 Hot/Daily
        if (text.endswith("?") or text.endswith("？")) and not (
            any(m in text for m in _PREF_MARKERS)
            or any(m in text for m in _AGENT_MARKERS)
        ):
            return []

        provenance = MemoryProvenance(source="reviewer", turn_id=turn.turn_id)
        scope = turn.scope

        if any(m in text for m in _AGENT_MARKERS):
            return [
                BuiltinMemoryMutation(
                    scope=scope,
                    target="memory",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        if any(m in text for m in _PREF_MARKERS):
            return [
                BuiltinMemoryMutation(
                    scope=scope,
                    target="profile",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        # 其余有实质内容的陈述 → 今日 daily（日期由 Manager Clock 决定）
        if len(text) >= 8:
            return [
                BuiltinMemoryMutation(
                    scope=scope,
                    target="daily",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        return []
