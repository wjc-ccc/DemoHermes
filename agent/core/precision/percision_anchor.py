"""Anchor 的模型 Adapter、内容验证、覆盖账本、生成重试和上下文投影。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from agent.types.context import Anchor, AnchorFact, ANCHOR_SECTIONS, render_anchor
from .context_item import AnchorContent, ContextItem, HistoryTurn, _LoadedContext
from agent.types.precision.priority import Priority
from .errors import AnchorBuildError, AnchorCompactionError, ContextBudgetExceededError
from agent.prompt.prompts.anchorExtraction import anchorExtractionPrompt

## anchor提示词设计


## anchor错误拦截器
## anchor单词调用生成 + 错误记录
class ModelAnchorBuilder:
    """使用无工具、单次模型调用生成 Anchor 五槽 JSON。"""

    def __init__(self, model) -> None:
        """注入模型，避免 PrecisionBuilder 自己创建远程依赖。"""
        self._model = model

    def __call__(self, payload: Mapping[str, object]) -> dict:
        """执行一次严格 JSON 压缩；重试和语义校验由 Engine 管理。"""
        try:
            response = self._model.chat(
                [
                    {"role": "system", "content": anchorExtractionPrompt},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                tools=None,
            )
        except Exception as exc:
            raise AnchorBuildError("anchor_model_error", repr(exc)) from exc

        try:
            # 如果解析失败尝试更高级别解析
            return _parse_json_object(_response_text(response))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AnchorBuildError("anchor_output_invalid", str(exc)) from exc


def _response_text(response: Any) -> str:
    """兼容统一 ModelResponse 和少量字典测试 Adapter。"""
    raw = getattr(response, "text", None)
    if raw is None and isinstance(response, dict):
        raw = response.get("text") or response.get("content")
    if not isinstance(raw, str):
        raise AnchorBuildError(
            "anchor_output_invalid", f"模型返回非文本: {type(raw)!r}"
        )
    return raw


def _parse_json_object(raw: str) -> dict:
    """剥离可容忍的 Markdown 围栏并要求顶层必须是对象。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AnchorBuilder 输出必须是 JSON 对象")
    return data


_ANCHOR_SLOTS = tuple(slot for slot, _ in ANCHOR_SECTIONS)
_ANCHOR_BUDGET_RATIO = 0.15
_ANCHOR_HARD_TOKEN_CAP = 2048


def coverage_turns(
    all_p2_turns: Sequence[HistoryTurn],
    already_covered: int,
    candidate_turns: Sequence[HistoryTurn],
) -> tuple[HistoryTurn, ...]:
    """返回从 Session 起点到新边界的连续完整前缀。"""
    coverage = tuple(
        all_p2_turns[: already_covered + len(candidate_turns)]
    )
    if not coverage:
        raise ContextBudgetExceededError()
    if not all(turn_starts_with_user(turn) for turn in coverage):
        raise AnchorCompactionError(
            "anchor_protocol_invalid", "待压缩前缀存在没有 user 起点的历史轮次"
        )
    return coverage


def try_build_anchor(
    loaded: _LoadedContext,
    *,
    builder: Callable[[Mapping[str, object]], Mapping[str, object]],
    coverage_turns: Sequence[HistoryTurn],
    candidate_turns: Sequence[HistoryTurn],
    max_tokens: int,
    estimate_item: Callable[[ContextItem], ContextItem],
) -> tuple[Anchor | None, ContextItem | None, int, str | None]:
    """最多调用 Builder 两次，并对结构、来源、错误和预算校验。"""
    allowed_ids = tuple(
        item.message_id or ""
        for turn in coverage_turns
        for item in turn
        if item.message_id
    )
    existing_content = (
        anchor_content(loaded.anchor)
        if loaded.anchor is not None
        else loaded.legacy_anchor_content
    )
    candidate_start = len(coverage_turns) - len(candidate_turns)
    new_turns = [
        anchor_turn_payload(turn, candidate_start + offset)
        for offset, turn in enumerate(candidate_turns)
    ]
    validation_error: str | None = loaded.anchor_error_code
    last_code: str | None = None

    for attempt in range(1, 3):
        target = max(1, max_tokens if attempt == 1 else max_tokens // 2)
        payload: dict[str, object] = {
            "existing_anchor": (
                anchor_content_payload(existing_content)
                if existing_content is not None
                else None
            ),
            "new_turns": new_turns,
            "allowed_message_ids": list(allowed_ids),
            "constraints": {
                "max_tokens": target,
                "validation_error_from_previous_attempt": validation_error,
            },
        }
        try:
            raw_content = builder(payload)
            content = validate_anchor_content(
                raw_content,
                allowed_ids=set(allowed_ids),
                coverage_turns=coverage_turns,
            )
            anchor = attach_anchor_coverage(
                content,
                session_id=loaded.session_id,
                coverage_turns=coverage_turns,
            )
            item = estimate_item(
                context_item_from_anchor(anchor, seq=anchor_sequence(loaded.history_turns))
            )
            if item.token_estimate > max_tokens:
                raise AnchorBuildError(
                    "anchor_too_large",
                    f"Anchor cost {item.token_estimate} > limit {max_tokens}",
                )
            return anchor, item, attempt, None
        except AnchorBuildError as exc:
            last_code = exc.code
            validation_error = f"{exc.code}: {exc.detail or str(exc)}"
    return None, None, 2, last_code or "anchor_output_invalid"


def turn_starts_with_user(turn: HistoryTurn) -> bool:
    """返回历史轮次是否具有可信 user 起点。"""
    return bool(turn and turn[0].message.get("role") == "user")


def coerce_anchor(value: Anchor | dict | None) -> tuple[Anchor | None, str | None]:
    """恢复全局 Anchor；损坏字典不阻断 Session 原文恢复。"""
    if value in (None, "", {}):
        return None, None
    if isinstance(value, Anchor):
        return value, None
    try:
        return Anchor.model_validate(value), None
    except (TypeError, ValueError, ValidationError):
        return None, "anchor_payload_invalid"


def validate_stored_anchor(
    anchor: Anchor,
    *,
    session_id: str,
    turns: Sequence[HistoryTurn],
) -> str | None:
    """证明已存 Anchor 恰好覆盖 Session 最旧的连续完整轮次。"""
    if anchor.session_id != session_id:
        return "anchor_session_mismatch"
    count = anchor.covered_turn_count
    if count <= 0 or count > len(turns):
        return "anchor_coverage_invalid"
    coverage = tuple(turns[:count])
    if (
        not coverage
        or not all(turn_starts_with_user(turn) for turn in coverage)
        or not all(
            item.priority == Priority.P2 for turn in coverage for item in turn
        )
    ):
        return "anchor_protocol_invalid"
    first = coverage[0][0]
    last = coverage[-1][-1]
    if (
        anchor.covered_start_seq != first.session_seq
        or anchor.covered_end_seq != last.session_seq
        or anchor.covered_start_message_id != first.message_id
        or anchor.covered_end_message_id != last.message_id
    ):
        return "anchor_coverage_invalid"
    digest = source_digest(coverage)
    if anchor.source_digest != digest:
        return "anchor_digest_mismatch"
    if anchor.anchor_id != anchor_id(session_id, last.message_id or "", digest):
        return "anchor_id_invalid"
    allowed = {
        item.message_id for turn in coverage for item in turn if item.message_id
    }
    for fact in anchor_facts(anchor):
        if not fact.source_message_ids or not set(fact.source_message_ids) <= allowed:
            return "anchor_source_invalid"
    return None


def validate_anchor_content(
    raw: Mapping[str, object],
    *,
    allowed_ids: set[str],
    coverage_turns: Sequence[HistoryTurn],
) -> AnchorContent:
    """严格校验 Builder 五槽结构、事实来源和必保工具错误。"""
    if not isinstance(raw, Mapping) or set(raw) != set(_ANCHOR_SLOTS):
        raise AnchorBuildError(
            "anchor_output_invalid",
            f"Anchor 字段必须恰好为 {list(_ANCHOR_SLOTS)}",
        )
    content: AnchorContent = {}
    for slot in _ANCHOR_SLOTS:
        values = raw.get(slot)
        if not isinstance(values, (list, tuple)):
            raise AnchorBuildError(
                "anchor_output_invalid", f"{slot} 必须是数组"
            )
        facts: list[AnchorFact] = []
        for value in values:
            try:
                fact = (
                    value
                    if isinstance(value, AnchorFact)
                    else AnchorFact.model_validate(value)
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise AnchorBuildError(
                    "anchor_output_invalid", f"{slot}: {exc}"
                ) from exc
            if not fact.text.strip():
                raise AnchorBuildError(
                    "anchor_output_invalid", "存在空白事实"
                )
            sources = set(fact.source_message_ids)
            if not sources or not sources <= allowed_ids:
                raise AnchorBuildError(
                    "anchor_source_invalid",
                    f"非法来源: {sorted(sources - allowed_ids)}",
                )
            facts.append(fact)
        content[slot] = tuple(facts)

    failed_sources = {
        source
        for fact in content["decisions"]
        if fact.status == "failed"
        for source in fact.source_message_ids
    }
    required_errors = {
        item.message_id
        for turn in coverage_turns
        for item in turn
        if item.is_error and item.message_id
    }
    missing = required_errors - failed_sources
    if missing:
        raise AnchorBuildError(
            "anchor_error_omitted",
            f"工具错误未进入 failed decisions: {sorted(missing)}",
        )
    return content


def attach_anchor_coverage(
    content: AnchorContent,
    *,
    session_id: str,
    coverage_turns: Sequence[HistoryTurn],
) -> Anchor:
    """由运行时给模型语义附加不可伪造的连续覆盖账本。"""
    first = coverage_turns[0][0]
    last = coverage_turns[-1][-1]
    digest = source_digest(coverage_turns)
    return Anchor(
        anchor_id=anchor_id(session_id, last.message_id or "", digest),
        session_id=session_id,
        covered_start_seq=int(first.session_seq or 0),
        covered_end_seq=int(last.session_seq or 0),
        covered_start_message_id=first.message_id or "",
        covered_end_message_id=last.message_id or "",
        covered_turn_count=len(coverage_turns),
        source_digest=digest,
        **content,
    )


def anchor_content(anchor: Anchor) -> AnchorContent:
    """返回不含覆盖账本的五槽内容。"""
    return {slot: getattr(anchor, slot) for slot in _ANCHOR_SLOTS}



def anchor_content_payload(content: AnchorContent) -> dict[str, list[dict]]:
    """把五槽内容序列化成 Builder 可消费的 JSON 对象。"""
    return {
        slot: [fact.model_dump(mode="json") for fact in content[slot]]
        for slot in _ANCHOR_SLOTS
    }


def anchor_facts(anchor: Anchor) -> tuple[AnchorFact, ...]:
    """按固定槽顺序返回 Anchor 全部事实。"""
    return tuple(fact for slot in _ANCHOR_SLOTS for fact in getattr(anchor, slot))



def parse_legacy_anchor(text: str) -> AnchorContent | None:
    """读取旧五槽文本作为迁移提示，但不建立覆盖关系。"""
    if "<context-anchor>" not in (text or ""):
        return None
    title_to_slot = {title: slot for slot, title in ANCHOR_SECTIONS}
    buckets: dict[str, list[AnchorFact]] = {
        slot: [] for slot in _ANCHOR_SLOTS
    }
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith(":") and line[:-1] in title_to_slot:
            current = title_to_slot[line[:-1]]
        elif current and line.startswith("- "):
            value = line[2:].strip()
            if value:
                buckets[current].append(AnchorFact(text=value))
    return {slot: tuple(buckets[slot]) for slot in _ANCHOR_SLOTS}


def anchor_turn_payload(turn: HistoryTurn, turn_index: int) -> dict:
    """把完整历史轮次转成一次性 Builder JSON。"""
    return {
        "turn_index": turn_index,
        "start_seq": int(turn[0].session_seq or 0),
        "end_seq": int(turn[-1].session_seq or 0),
        "messages": [
            {
                "message_id": item.message_id or "",
                "seq_id": int(item.session_seq or 0),
                "role": str(item.message.get("role") or ""),
                "content": str(item.message.get("content") or ""),
                "tool_calls": list(item.message.get("tool_calls") or ()),
                "is_error": item.is_error,
            }
            for item in turn
        ],
    }


def source_digest(turns: Sequence[HistoryTurn]) -> str:
    """对覆盖原文的身份、顺序和 Session 原文内容计算稳定摘要。"""
    payload = [
        {
            "message_id": item.message_id,
            "seq_id": item.session_seq,
            "message": item.raw_message if item.raw_message is not None else item.message,
        }
        for turn in turns
        for item in turn
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def anchor_id(session_id: str, end_message_id: str, digest: str) -> str:
    """根据 Session、覆盖边界和摘要生成确定性 Anchor 标识。"""
    raw = f"{session_id}:{end_message_id}:{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def anchor_sequence(turns: Sequence[HistoryTurn]) -> int:
    """返回 Anchor 在统一上下文中位于所有 Session 历史之前的位置。"""
    if not turns or not turns[0]:
        return 0
    return turns[0][0].seq - 1


def context_item_from_anchor(anchor: Anchor, *, seq: int) -> ContextItem:
    """把全局 Anchor 转成优先于历史原文的 P2-A 原子。"""
    return ContextItem(
        name="p2_anchor",
        kind="p2_anchor",
        priority=Priority.P2,
        message={"role": "system", "content": render_anchor(anchor)},
        seq=seq,
    )


def anchor_token_limit(cap: int) -> int:
    """返回 Anchor 的 15% 动态上限和 2048 token 硬上限。"""
    return max(1, min(int(cap * _ANCHOR_BUDGET_RATIO), _ANCHOR_HARD_TOKEN_CAP))
