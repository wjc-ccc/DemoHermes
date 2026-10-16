"""Anchor 的模型 Adapter、内容验证、覆盖账本、生成重试和上下文投影。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from agent.types.context import Anchor, AnchorFact, ANCHOR_SECTIONS, render_anchor
from .context_item import AnchorContent, ContextItem, HistoryTurn, _LoadedContext
from agent.types.perception.priority import Priority
from .errors import AnchorBuildError, AnchorCompactionError, ContextBudgetExceededError
from agent.core.prompt.anchorExtraction import anchorExtractionPrompt
from agent.core.prompt.renderers import render_anchor_input
from agent.types.perception.trace import ModelCallSnapshot, normalized_usage, response_metadata
from agent.core.model.call_options import structured_call_options
from agent.types.perception.result import PerceptionConfig

## anchor提示词设计

_ANCHOR_SPLIT_BYTES = 16 * 1024
_ANCHOR_MAX_TURNS_PER_PART = 8
_ANCHOR_MAX_WORKERS = 3


@dataclass(frozen=True)
class _AnchorCallResult:
    """单次模型调用的结果与账本，由调用线程统一接收。"""

    content: dict | None
    snapshot: ModelCallSnapshot
    error: AnchorBuildError | None


def _split_anchor_turns(turns: Sequence[dict]) -> tuple[tuple[dict, ...], ...]:
    """按完整轮次远粗近细切段；短输入保持一次调用。"""
    turns = tuple(turns)
    size = sum(
        len(str(message.get("content") or "").encode("utf-8"))
        + len(json.dumps(message.get("tool_calls") or [], ensure_ascii=False).encode("utf-8"))
        for turn in turns for message in turn["messages"]
    )
    if size <= _ANCHOR_SPLIT_BYTES or len(turns) < 3:
        return (turns,)
    remaining = len(turns)
    recent_sizes = []
    width = 1
    while width < _ANCHOR_MAX_TURNS_PER_PART and remaining >= 2 * width:
        recent_sizes.append(width)
        remaining -= width
        width *= 2

    # 更旧历史也按八轮封顶；尾部余量过小时，平衡相邻旧段以保持远粗近细。
    count = (remaining + _ANCHOR_MAX_TURNS_PER_PART - 1) // _ANCHOR_MAX_TURNS_PER_PART
    older_sizes = [_ANCHOR_MAX_TURNS_PER_PART] * (count - 1)
    older_sizes.append(remaining - sum(older_sizes))
    if len(older_sizes) > 1 and older_sizes[-1] < recent_sizes[-1]:
        moved = recent_sizes[-1] - older_sizes[-1]
        older_sizes[-2] -= moved
        older_sizes[-1] += moved

    parts = []
    start = 0
    for size in (*older_sizes, *reversed(recent_sizes)):
        parts.append(turns[start:start + size])
        start += size
    return tuple(parts)


def _reference_turns(turns: Sequence[dict]) -> tuple[list[dict], dict[str, tuple[str, ...]]]:
    """只在模型输入副本中使用短消息编号，保留真实消息到来源映射。"""
    aliases: dict[str, str] = {}
    references: dict[str, tuple[str, ...]] = {}
    result = []
    for turn in turns:
        messages = []
        for message in turn["messages"]:
            message_id = message.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                raise AnchorBuildError("anchor_source_invalid", "历史消息缺少来源 ID")
            if message_id not in aliases:
                alias = f"m{len(aliases) + 1}"
                aliases[message_id] = alias
                references[alias] = (message_id,)
            messages.append({**message, "message_id": aliases[message_id]})
        result.append({**turn, "messages": messages})
    return result, references


def _reference_facts(content: Mapping | None, references: dict[str, tuple[str, ...]]) -> dict | None:
    """为已有和中间事实分配短编号，合并时继承完整来源集合。"""
    if content is None:
        return None
    result = {}
    for slot, _ in ANCHOR_SECTIONS:
        facts = []
        for value in content.get(slot, ()):
            fact = AnchorFact.model_validate(value)
            view = fact.model_dump(mode="json")
            if fact.source_message_ids:
                alias = f"f{len(references) + 1}"
                references[alias] = fact.source_message_ids
                view.update(source_ref=alias, source_message_ids=[alias])
            facts.append(view)
        result[slot] = facts
    return result


def _resolve_anchor_sources(raw: dict, references: Mapping[str, tuple[str, ...]]) -> dict:
    """严格展开模型选择的短引用；不根据事实文本猜测来源。"""
    result = dict(raw)
    for slot, _ in ANCHOR_SECTIONS:
        if slot not in raw:
            continue
        values = raw[slot]
        if not isinstance(values, (list, tuple)):
            raise AnchorBuildError("anchor_output_invalid", f"{slot} 必须是数组")
        facts = []
        for value in values:
            try:
                fact = AnchorFact.model_validate(value)
            except (TypeError, ValueError, ValidationError) as exc:
                raise AnchorBuildError("anchor_output_invalid", f"{slot}: {exc}") from exc
            if not fact.source_message_ids or any(ref not in references for ref in fact.source_message_ids):
                raise AnchorBuildError("anchor_source_invalid", "事实含空来源或未知短引用")
            sources = tuple(dict.fromkeys(
                source for ref in fact.source_message_ids for source in references[ref]
            ))
            facts.append({**fact.model_dump(mode="json"), "source_message_ids": list(sources)})
        result[slot] = facts
    return result


## anchor错误拦截器
## anchor单词调用生成 + 错误记录
class ModelAnchorBuilder:
    """使用无工具模型生成五槽 JSON，长输入在内部并发提取后逐槽合并。"""

    def __init__(self, model, *, provider_max_output_tokens: int | None = None) -> None:
        """注入模型，避免 PerceptionBuilder 自己创建远程依赖。"""
        if provider_max_output_tokens is not None and (
            isinstance(provider_max_output_tokens, bool)
            or not isinstance(provider_max_output_tokens, int)
            or provider_max_output_tokens <= 0
        ):
            raise ValueError("provider_max_output_tokens 必须为正整数或 None")
        self._model = model
        self._provider_max_output_tokens = provider_max_output_tokens
        self._calls = ContextVar(f"anchor_calls_{id(self)}", default=())

    def begin_capture(self):
        self._calls.set(())

    def take_calls(self):
        calls = self._calls.get()
        self._calls.set(())
        return calls

    def __call__(self, payload: Mapping[str, object]) -> dict:
        """保持一次 Builder 的输入输出契约；整次重试和最终校验由 Engine 管理。"""
        turns, message_refs = _reference_turns(payload.get("new_turns") or ())
        fact_refs: dict[str, tuple[str, ...]] = {}
        existing = _reference_facts(payload.get("existing_anchor"), fact_refs)
        parts = _split_anchor_turns(turns)
        constraints = dict(payload.get("constraints") or {})
        if len(parts) == 1:
            outcome = self._call_model({
                **payload, "existing_anchor": existing, "new_turns": turns,
                "allowed_message_ids": [*message_refs, *fact_refs],
                "constraints": {**constraints, "phase": "compress"},
            })
            self._record_calls((outcome,))
            if outcome.error is not None:
                raise outcome.error
            return _resolve_anchor_sources(outcome.content, {**message_refs, **fact_refs})

        jobs = [{
            "existing_anchor": None, "new_turns": part,
            "allowed_message_ids": [message["message_id"] for turn in part for message in turn["messages"]],
            "constraints": {
                **constraints, "phase": "extract",
                "max_tokens": self._provider_max_output_tokens or constraints.get("max_tokens"),
            },
        } for part in parts]
        # 工作线程只返回结果；ContextVar 的写入统一留在本次 Builder 的调用线程。
        with ThreadPoolExecutor(max_workers=min(_ANCHOR_MAX_WORKERS, len(jobs))) as pool:
            outcomes = tuple(pool.map(self._call_model, jobs))
        self._record_calls(outcomes)
        groups = []
        for index, (part, job, outcome) in enumerate(zip(parts, jobs, outcomes), 1):
            if outcome.error is not None:
                raise outcome.error
            local_refs = {ref: message_refs[ref] for ref in job["allowed_message_ids"]}
            resolved = _resolve_anchor_sources(outcome.content, local_refs)
            content = validate_anchor_content(
                resolved,
                allowed_ids={source for ids in local_refs.values() for source in ids},
                coverage_turns=(),
            )
            required_errors = {
                message_refs[message["message_id"]][0]
                for turn in part for message in turn["messages"] if message.get("is_error")
            }
            failed_sources = {
                source for fact in content["decisions"]
                if fact.status == "failed" for source in fact.source_message_ids
            }
            if required_errors - failed_sources:
                raise AnchorBuildError("anchor_error_omitted", "分段提取遗漏工具错误来源")
            groups.append({
                "label": f"part-{index}",
                "start_seq": part[0]["start_seq"], "end_seq": part[-1]["end_seq"],
                **_reference_facts(anchor_content_payload(content), fact_refs),
            })
        outcome = self._call_model({
            "existing_anchor": existing, "new_turns": [], "extraction_groups": groups,
            "allowed_message_ids": list(fact_refs),
            "constraints": {**constraints, "phase": "merge"},
        })
        self._record_calls((outcome,))
        if outcome.error is not None:
            raise outcome.error
        return _resolve_anchor_sources(outcome.content, fact_refs)

    def _record_calls(self, outcomes: Sequence[_AnchorCallResult]) -> None:
        """按提交顺序记录每次真实调用，包括并发失败，且不跨会话共享列表。"""
        calls = self._calls.get()
        snapshots = tuple(
            outcome.snapshot.model_copy(update={"attempt": len(calls) + index})
            for index, outcome in enumerate(outcomes, 1)
        )
        self._calls.set((*calls, *snapshots))

    def _call_model(self, payload: Mapping[str, object]) -> _AnchorCallResult:
        """单次调用返回内容、失败和快照，不在工作线程修改采集上下文。"""
        started = time.time()
        response = None
        error = None
        failure = None
        content = None
        try:
            requested_limit = (payload.get("constraints") or {}).get("max_tokens")
            if (payload.get("constraints") or {}).get("phase") == "extract" and self._provider_max_output_tokens is not None:
                requested_limit = None
            # 无语义上限时仅采用实际服务端输出能力，不另传固定 token 上限。
            if requested_limit is None:
                if self._provider_max_output_tokens is None:
                    raise ValueError("无 Anchor 输出上限时必须提供 provider_max_output_tokens")
                output_limit = self._provider_max_output_tokens
            else:
                output_limit = max(256, int(requested_limit))
                if self._provider_max_output_tokens is not None:
                    output_limit = min(output_limit, self._provider_max_output_tokens)
            response = self._model.chat(
                [
                    {"role": "system", "content": anchorExtractionPrompt},
                    {
                        "role": "user",
                        "content": render_anchor_input(payload),
                    },
                ],
                tools=None,
                max_tokens=output_limit,
                **structured_call_options(self._model),
            )
        except Exception as exc:
            error = str(exc)
            failure = AnchorBuildError("anchor_model_error", repr(exc))
        if failure is None:
            try:
                # 如果解析失败尝试更高级别解析
                content = _parse_json_object(_response_text(response))
            except (AnchorBuildError, TypeError, ValueError) as exc:
                error = str(exc)
                failure = exc if isinstance(exc, AnchorBuildError) else AnchorBuildError("anchor_output_invalid", str(exc))
        finished = time.time()
        usage = dict(getattr(response, "usage", None) or {})
        incoming, outgoing = normalized_usage(usage)
        snapshot = ModelCallSnapshot(
            attempt=1, purpose="anchor", provider=getattr(self._model, "name", None),
            model=getattr(self._model, "model_name", None), started_at=started, finished_at=finished,
            duration_ms=max(0, int((finished-started)*1000)), usage=usage, error=error,
            input_token_count=incoming, output_token_count=outgoing, **response_metadata(response),
        )
        return _AnchorCallResult(content, snapshot, failure)


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
    # Some providers wrap a single fact or group arrays under labels. Only
    # normalize containers; strict fact fields, provenance and budget checks
    # still run in validate_anchor_content.
    def facts(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            if "text" in value:
                return [value]
            return [fact for group in value.values() for fact in facts(group)]
        raise ValueError("Anchor group must contain fact arrays")
    for slot in _ANCHOR_SLOTS:
        if isinstance(data.get(slot), dict):
            data[slot] = facts(data[slot])
    return data


_ANCHOR_SLOTS = tuple(slot for slot, _ in ANCHOR_SECTIONS)


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
    max_tokens: int | None,
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
        target = None if max_tokens is None else max(1, max_tokens if attempt == 1 else max_tokens // 2)
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
            if max_tokens is not None and item.token_estimate > max_tokens:
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


def anchor_token_limit(cap: int, *, ratio: float | None = None) -> int:
    """返回 Anchor 的动态比例上限。默认是输入预算的 15%。"""
    defaults = PerceptionConfig()
    ratio = defaults.anchor_budget_ratio if ratio is None else ratio
    return max(1, int(cap * ratio))
