"""LlmEvolutionReflector — 业界完整形态：模型复盘轨迹 → playbook + skill 提案。

模型是外部 seam；测试注入 Fake。失败时降级为空列表（不阻断旁路）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..memory.types import MemoryScope, TurnRecord
from .types import EvolutionInsight, EvolutionSignal, SkillProposal

logger = logging.getLogger(__name__)

PLAYBOOK_PREFIX = "[playbook]"


class LlmEvolutionReflector:
    """调用 model.chat 解析 JSON insights；可与 RuleBased 并联。"""

    def __init__(self, model, *, store=None) -> None:
        self._model = model
        self._store = store

    def reflect(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None = None,
        *,
        user_text: str = "",
        transcript: str = "",
    ) -> list[EvolutionInsight]:
        prompt = self._build_prompt(turn, signals, user_text, transcript)
        try:
            resp = self._model.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
            )
            text = getattr(resp, "text", "") or ""
        except Exception:
            logger.exception("LLM Reflector 调用失败 turn_id=%s", turn.turn_id)
            return []
        return self._parse_insights(text, turn.scope, turn.turn_id)

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
        return self._parse_skills(text, turn.turn_id)

    def _build_prompt(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None,
        user_text: str,
        transcript: str,
    ) -> str:
        sig = ""
        if signals is not None:
            sig = (
                f"failures={list(signals.tool_failures)}; "
                f"tools={list(signals.tool_sequence)}; "
                f"recovered={signals.recovered_from_failure}"
            )
        return (
            "你是 Agent 策略复盘器。根据本回合信息，输出 JSON 数组，"
            "每项含 content(以[playbook]开头的可执行策略)、rationale。\n"
            "只输出 JSON，不要其它文字。无策略则输出 []。\n"
            f"user: {user_text[:500]}\n"
            f"signals: {sig}\n"
            f"transcript: {transcript[:2000]}\n"
            f"error: {turn.error}\n"
        )

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

    def _parse_insights(
        self, text: str, scope: MemoryScope, turn_id: str,
    ) -> list[EvolutionInsight]:
        data = _extract_json(text)
        if not isinstance(data, list):
            return []
        out: list[EvolutionInsight] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if not content.startswith(PLAYBOOK_PREFIX):
                content = f"{PLAYBOOK_PREFIX} {content}"
            out.append(
                EvolutionInsight(
                    scope=scope,
                    op="add",
                    content=content,
                    rationale=str(item.get("rationale") or "llm_reflect"),
                    turn_id=turn_id,
                )
            )
        return out

    def _parse_skills(self, text: str, turn_id: str) -> list[SkillProposal]:
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
