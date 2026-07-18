"""
ContextBuilder - LLM 上下文组装器

将多层信息按优先级拼成发给 LLM 的 messages 列表：
    1. System Prompt（prompt/systemPrompt.md）
    2. Skill Level 1 摘要（可用技能列表，引导 LLM 用 use_skill 加载）
    3. Ephemeral 平台说明（channel_prompt，仅当次注入，不落 Session）
    4. 当前对话消息（user / assistant / tool，映射为 OpenAI 协议格式）

消息映射规则（OpenAI function calling 协议）：
    assistant 带工具调用 → {"role": "assistant", "content": ..., "tool_calls": [...]}
    工具结果             → {"role": "tool", "tool_call_id": ..., "content": ...}

后续挂载点（保持 building_context 签名不变）：
    Hot Memory 快照、Episodic 检索结果、token 长度控制。
"""
from __future__ import annotations

import json
import logging

from .types import Message, Session
from ..provider import SYSTEM_PROMPT_PATH

logger = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(
        self,
        session: Session,
        *,
        skill_registry=None,
        ephemeral_prompt: str | None = None,
    ):
        self.session: Session = session
        # Level 1 技能摘要（无技能注册表时为空串，不占 prompt）
        self._skill_summary: str = (
            skill_registry.summaries_prompt() if skill_registry else ""
        )
        # 仅当次生效的平台说明（如「你在飞书群里」），不写入 Session.messages
        self._ephemeral_prompt = ephemeral_prompt
        self.messages: list[dict] = []  # 组装后发给 LLM 的 messages

    # ---- 1. system prompt ----
    def _loading_system_prompt(self) -> None:
        try:
            content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("system prompt 文件缺失: %s，使用兜底文案", SYSTEM_PROMPT_PATH)
            content = "你是一个友好的AI助手。"
        self.messages.append({"role": "system", "content": content})

    # ---- 2. 技能摘要（Level 1）----
    def _loading_skills(self) -> None:
        if self._skill_summary:
            # 并入 system 角色，避免占用对话轮次
            self.messages.append({"role": "system", "content": self._skill_summary})

    # ---- 3. ephemeral 平台说明 ----
    def _loading_ephemeral(self) -> None:
        if self._ephemeral_prompt:
            self.messages.append({"role": "system", "content": self._ephemeral_prompt})

    # ---- 4. 对话消息（含 tool 协议映射）----
    def _loading_messages(self) -> None:
        for m in self.session.messages:
            self.messages.append(self._map_message(m))

    @staticmethod
    def _map_message(m: Message) -> dict:
        """Session 内部消息 → OpenAI API 格式。"""
        # 助手消息带工具调用：必须还原 tool_calls 字段，否则协议不完整
        if m.role == "assistant" and m.metadata.get("tool_calls"):
            return {
                "role": "assistant",
                "content": m.content_text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc.get("arguments_raw") or "{}",
                        },
                    }
                    for tc in m.metadata["tool_calls"]
                ],
            }
        # 工具结果消息：tool_call_id 与上面的调用 id 一一对应
        if m.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": m.metadata.get("tool_call_id", ""),
                "content": m.content_text,
            }
        return {"role": m.role, "content": m.content_text}

    # ---- 组装入口 ----
    def building_context(self) -> list[dict]:
        self.messages = []  # 每轮重置
        self._loading_system_prompt()
        self._loading_skills()
        self._loading_ephemeral()
        self._loading_messages()
        return self.messages

    # ---- 统计（观测用）----
    def context_stats(self) -> dict:
        """粗略统计当前上下文规模，供事件/日志观测。"""
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in self.messages)
        return {"message_count": len(self.messages), "total_chars": total_chars}
