"""MemoryExtractor — 模型抽取 + 规则降级。

Reviewer 只消费 Extractor 产出的 BuiltinMemoryRequest，经 Manager.apply 落库：
  - profile：用户稳定偏好/身份
  - memory：对助手的长期约束
  - daily：当日事件
  - 招呼/一次性问答 → 不写（输出 []）

生产默认：FallbackMemoryExtractor(ModelBasedExtractor, RuleBasedExtractor)
先模型；调用/解析失败再关键词规则。合法空数组 [] 不降级。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .types import BuiltinMemoryRequest, MemoryProvenance, TurnRecord

logger = logging.getLogger(__name__)

_PREF_MARKERS = (
    "偏好", "喜欢", "请记住", "记住我", "以后请", "以后都", "总是",
    "不要再", "我叫", "我的名字", "习惯",
)
_AGENT_MARKERS = (
    "你必须", "请始终", "你是", "记住你", "作为助手", "回答时请",
)
_GREETINGS = {"你好", "您好", "hi", "hello", "hey", "在吗", "早上好", "晚上好"}

_ALLOWED_TARGETS = frozenset({"profile", "memory", "daily"})
_SYSTEM_PROMPT = (
    "你是记忆提炼器。根据本回合用户话语与有限上下文，判断是否写入长期记忆。\n"
    "分层：\n"
    "- profile：用户稳定偏好/身份事实\n"
    "- memory：用户对助手的长期约束或角色要求\n"
    "- daily：当日有价值的事件/进展（非长期偏好）\n"
    "招呼、一次性问答、敏感机密、工具原始流水 → 不写。\n"
    "只输出 JSON 数组，不要其它文字。无记忆则输出 []。\n"
    '每项格式：{"target":"profile"|"memory"|"daily","action":"add","content":"简洁陈述"}\n'
    "action 只能是 add；content 用第三人称简洁改写，不要原样堆砌整段闲聊。"
)


class ModelExtractionError(Exception):
    """模型抽取失败（供 Fallback 降级到规则抽取）。"""


class RuleBasedExtractor:
    """关键词启发式抽取；作 ModelBased 失败时的降级。"""

    def __init__(self, store) -> None:
        self._store = store

    def extract(self, turn: TurnRecord) -> list[BuiltinMemoryRequest]:
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

        if (text.endswith("?") or text.endswith("？")) and not (
            any(m in text for m in _PREF_MARKERS)
            or any(m in text for m in _AGENT_MARKERS)
        ):
            return []

        provenance = MemoryProvenance(source="reviewer", turn_id=turn.turn_id)
        scope = turn.scope

        if any(m in text for m in _AGENT_MARKERS):
            return [
                BuiltinMemoryRequest(
                    scope=scope,
                    target="memory",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        if any(m in text for m in _PREF_MARKERS):
            return [
                BuiltinMemoryRequest(
                    scope=scope,
                    target="profile",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        if len(text) >= 8:
            return [
                BuiltinMemoryRequest(
                    scope=scope,
                    target="daily",
                    action="add",
                    content=text,
                    provenance=provenance,
                )
            ]
        return []


class ModelBasedExtractor:
    """调用 chat 模型，解析固定 JSON 数组 → BuiltinMemoryRequest。"""

    def __init__(self, store, model) -> None:
        self._store = store
        self._model = model

    def extract(self, turn: TurnRecord) -> list[BuiltinMemoryRequest]:
        if turn.interrupted or not turn.completed:
            return []

        session = self._store.get_session(turn.session_id)
        if session is None:
            raise ModelExtractionError(f"session 不存在 id={turn.session_id}")

        user = next(
            (m for m in session.messages if m.message_id == turn.user_message_id),
            None,
        )
        if user is None:
            return []

        text = (user.content_text or "").strip()
        if not text or text.startswith("/"):
            return []

        # 短上下文：前后各取有限消息，避免灌整段 session
        ctx_lines: list[str] = []
        for m in session.messages[-8:]:
            role = m.role
            body = (m.content_text or "").strip()
            if body:
                ctx_lines.append(f"{role}: {body[:400]}")
        user_block = "\n".join(ctx_lines) or f"user: {text}"

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ]
        try:
            resp = self._model.chat(messages)
        except Exception as exc:
            raise ModelExtractionError(repr(exc)) from exc

        raw = getattr(resp, "text", None)
        if raw is None and isinstance(resp, dict):
            raw = resp.get("text") or resp.get("content")
        if not isinstance(raw, str):
            raise ModelExtractionError(f"模型返回非文本: {type(resp)!r}")

        items = _parse_extraction_json(raw)
        provenance = MemoryProvenance(source="reviewer", turn_id=turn.turn_id)
        out: list[BuiltinMemoryRequest] = []
        for item in items:
            req = _item_to_request(item, scope=turn.scope, provenance=provenance)
            if req is not None:
                out.append(req)
        return out


class FallbackMemoryExtractor:
    """先 primary；ModelExtractionError 时走 fallback。合法 [] 不降级。"""

    def __init__(self, primary, fallback) -> None:
        self._primary = primary
        self._fallback = fallback

    def extract(self, turn: TurnRecord) -> list[BuiltinMemoryRequest]:
        try:
            return self._primary.extract(turn)
        except ModelExtractionError as exc:
            logger.warning("模型抽取失败，降级规则抽取: %s", exc)
            return self._fallback.extract(turn)


def _parse_extraction_json(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 尝试截取首个数组
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            raise ModelExtractionError(f"JSON 解析失败: {exc}") from exc
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc2:
            raise ModelExtractionError(f"JSON 解析失败: {exc2}") from exc2
    if not isinstance(data, list):
        raise ModelExtractionError("模型输出必须是 JSON 数组")
    return data


def _item_to_request(item, *, scope, provenance) -> BuiltinMemoryRequest | None:
    if not isinstance(item, dict):
        return None
    target = item.get("target")
    action = item.get("action", "add")
    content = (item.get("content") or "").strip()
    if target not in _ALLOWED_TARGETS or action != "add" or not content:
        return None
    return BuiltinMemoryRequest(
        scope=scope,
        target=target,
        action="add",
        content=content,
        provenance=provenance,
    )
