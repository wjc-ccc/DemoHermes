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
from typing import TYPE_CHECKING

from .context_budget import (
    ContextBudget,
    ContextBudgetExceededError,
    TokenEstimator,
    trim_to_budget,
)
from .types import Message, Session
from ..Provider import SYSTEM_PROMPT_PATH

if TYPE_CHECKING:
    from ..memory.snapshot import MemoryContextSnapshot

logger = logging.getLogger(__name__)

_MESSAGE_OVERHEAD_BYTES = 16  # role/协议字段的保守固定开销


class _ConservativeEstimator:
    """§4.3 兜底估算：JSON 序列化 UTF-8 字节数 + 每消息固定 overhead。"""

    def estimate_messages(self, messages) -> int:
        return sum(
            len(json.dumps(m, ensure_ascii=False).encode("utf-8")) + _MESSAGE_OVERHEAD_BYTES
            for m in messages
        )


class ContextBuilder:
    def __init__(
        self,
        session: Session,
        *,
        memory_context: MemoryContextSnapshot | None = None,
        skill_registry=None,
        ephemeral_prompt: str | None = None,
        budget: ContextBudget | None = None,
        token_estimator: TokenEstimator | None = None,
        provider_min_recall_chars: int | None = None,
        skill_summary_min_chars: int | None = None,
    ):
        self.session: Session = session
        # 本 turn 的不可变记忆快照（7.23 方案 §2 Seam C）：
        # 只读取预构建的 prompt 整体注入，不分辨 Hot/Daily/Provider，也不知道文件/SQLite
        self._memory_context = memory_context
        # Level 1 技能摘要（无技能注册表时为空串，不占 prompt）
        self._skill_summary: str = (
            skill_registry.summaries_prompt() if skill_registry else ""
        )
        # 仅当次生效的平台说明（如「你在飞书群里」），不写入 Session.messages
        self._ephemeral_prompt = ephemeral_prompt
        # §4.3 整轮预算：None 表示不启用裁剪（现状行为）
        self._budget = budget
        self._token_estimator = token_estimator
        # 裁剪顺序 2 的 Provider recall 最小配额；None 表示不做缩短步骤
        self._provider_min_recall_chars = provider_min_recall_chars
        # 裁剪顺序 4：Skill 摘要最小配额；None 表示不做该步
        self._skill_summary_min_chars = skill_summary_min_chars
        self.messages: list[dict] = []  # 组装后发给 LLM 的 messages
        self._tagged: list[tuple[str, dict]] = []  # 预算裁剪用的带分类副本

    def _append(self, kind: str, message: dict) -> None:
        self.messages.append(message)
        self._tagged.append((kind, message))

    # ---- 1. system prompt ----
    def _loading_system_prompt(self) -> None:
        try:
            content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("system prompt 文件缺失: %s，使用兜底文案", SYSTEM_PROMPT_PATH)
            content = "你是一个友好的AI助手。"
        self._append("system_prompt", {"role": "system", "content": content})

    # ---- 1.5 记忆快照（整段带围栏注入）----
    def _loading_memory(self) -> None:
        if self._memory_context is None:
            return
        if self._budget is None:
            # 无预算：整段注入完整 prompt（现状行为）
            self._append(
                "memory_hot",
                {"role": "system", "content": self._memory_context.prompt},
            )
            return
        # §4.3 启用预算：结构化分段注入，裁剪才能区分 Hot/Daily/Provider
        snapshot = self._memory_context
        hot = snapshot.hot_prompt if snapshot.hot_prompt is not None else snapshot.prompt
        self._append("memory_hot", {"role": "system", "content": hot})
        if snapshot.daily_prompt:
            self._append(
                "memory_daily", {"role": "system", "content": snapshot.daily_prompt}
            )
        if snapshot.provider_prompt:
            self._append(
                "memory_provider",
                {"role": "system", "content": snapshot.provider_prompt},
            )

    # ---- 2. 技能摘要（Level 1）----
    def _loading_skills(self) -> None:
        if self._skill_summary:
            # 并入 system 角色，避免占用对话轮次
            self._append("skill", {"role": "system", "content": self._skill_summary})

    # ---- 3. ephemeral 平台说明 ----
    def _loading_ephemeral(self) -> None:
        if self._ephemeral_prompt:
            self._append(
                "ephemeral", {"role": "system", "content": self._ephemeral_prompt}
            )

    # ---- 4. 对话消息（含 tool 协议映射）----
    def _loading_messages(self) -> None:
        for m in self.session.messages:
            self._append("history", self._map_message(m))

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
        self._tagged = []
        self._loading_system_prompt()
        self._loading_memory()
        self._loading_skills()
        self._loading_ephemeral()
        self._loading_messages()
        if self._budget is not None:
            # §4.3：每次发模型前检查最终 messages，而不只统计记忆快照
            trimmed = trim_to_budget(
                self._tagged, self._budget, self._estimator(),
                provider_min_recall_chars=self._provider_min_recall_chars,
                skill_summary_min_chars=self._skill_summary_min_chars,
            )
            if trimmed is None:
                raise ContextBudgetExceededError()
            self.messages = trimmed
        return self.messages

    def _estimator(self) -> TokenEstimator:
        """未提供 tokenizer 时的保守估算：序列化 UTF-8 字节数 + 每消息固定 overhead。

        允许过度裁剪，不允许乐观低估（§4.3）。
        """
        if self._token_estimator is not None:
            return self._token_estimator
        return _ConservativeEstimator()

    # ---- 统计（观测用）----
    def context_stats(self) -> dict:
        """粗略统计当前上下文规模，供事件/日志观测。"""
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in self.messages)
        return {"message_count": len(self.messages), "total_chars": total_chars}
