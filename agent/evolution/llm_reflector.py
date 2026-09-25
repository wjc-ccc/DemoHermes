"""LlmEvolutionReflector — 模型复盘轨迹 → Skill 提案。

模型是外部 seam；测试注入 Fake。失败时降级为空列表（不阻断旁路）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..core.memory.types import TurnRecord
from .types import EvolutionSignal, SkillProposal

logger = logging.getLogger(__name__)

class LlmEvolutionReflector:
    """调用 model.chat 解析 JSON Skill 提案。"""

    def __init__(self, model, *, store=None) -> None:
        self._model = model
        self._store = store

    def reflect_skills(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None = None,
        *,
        user_text: str = "",
        transcript: str = "",
    ) -> list[SkillProposal]:
        prompt = self._build_skill_prompt(turn, signals, user_text, transcript)
        try:
            resp = self._model.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
            )
            text = getattr(resp, "text", "") or ""
        except Exception:
            logger.exception("LLM Skill Reflector 失败 turn_id=%s", turn.turn_id)
            return []
        return self._parse_skills(text, turn.turn_id, scope_key=turn.scope.storage_key)

    def _build_skill_prompt(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None,
        user_text: str,
        transcript: str,
    ) -> str:
        tools = list(signals.tool_sequence) if signals else []
        return (
            "将可复用多步流程沉淀为 Skill。输出 JSON 对象："
            '{"name","description","body"}；body 含 ## 目标/步骤/失败处理/验证。\n'
            "不可复用则输出 null。只输出 JSON。\n"
            f"user: {user_text[:500]}\n"
            f"tools: {tools}\n"
            f"transcript: {transcript[:2000]}\n"
        )

    def _parse_skills(
        self, text: str, turn_id: str, *, scope_key: str = ""
    ) -> list[SkillProposal]:
        data = _extract_json(text)
        if data is None or not isinstance(data, dict):
            return []
        name = str(data.get("name") or "").strip()
        description = str(data.get("description") or "").strip()
        body = str(data.get("body") or "").strip()
        if not (name and description and body):
            return []
        return [
            SkillProposal(
                name=name,
                description=description,
                body=body,
                status="draft",
                turn_id=turn_id,
                rationale="llm_skill_reflect",
                scope_key=scope_key,
            )
        ]


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
