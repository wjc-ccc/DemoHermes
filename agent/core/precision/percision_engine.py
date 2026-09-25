"""固定 Precision 主链：加载 → 分级 → 构建。"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from pydantic import ValidationError

from ...provider import SYSTEM_PROMPT_PATH
from ..context_budget import ContextBudget, ContextBudgetExceededError, TokenEstimator
from ..types import Anchor, AnchorFact, Message, Session
from .percision_anchor import AnchorBuildError, render_anchor
from .percision_types import (
    AnchorCompactionError,
    AnchorStatus,
    ContextItem,
    Decision,
    PrecisionMetrics,
    PrecisionRecord,
    PrecisionResult,
    Priority,
)

if TYPE_CHECKING:
    from ..memory import MemorySnapshot
    from ...skills.registry import SkillSelection
    from .percision_trace import PrecisionTrace

logger = logging.getLogger(__name__)

## 固定对应的权限级别
_P0_KINDS = frozenset({"system_prompt", "skill", "ephemeral", "memory_hot"})
_P3_KINDS = frozenset({"memory_daily", "memory_provider"})
_ANCHOR_SLOTS = ("intent", "changes", "decisions", "next_steps", "excluded")
_TOOL_FAIL_PREFIX = "工具执行失败"
_KEEP_RECENT_P2_TURNS = 2
_COMPACT_RATIO = 0.85
_ANCHOR_BUDGET_RATIO = 0.15
_ANCHOR_HARD_TOKEN_CAP = 2048
_P0_SHARE_WARNING = 0.10
_UNLIMITED = 2**31 - 1
_MESSAGE_OVERHEAD_BYTES = 16

AnchorContent = dict[str, tuple[AnchorFact, ...]]
HistoryTurn = tuple[ContextItem, ...]


class _ConservativeEstimator:
    """没有模型 tokenizer 时按 UTF-8 bytes 保守估算。"""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        """返回整批消息的保守字节成本，并计入协议开销。"""
        return sum(
            len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            + _MESSAGE_OVERHEAD_BYTES
            for message in messages
        )

## load阶段产物
@dataclass(frozen=True)
class _LoadedContext:
    """load 阶段的完整产物，包含统一原子和原始历史轮次。"""

    session_id: str
    items: tuple[ContextItem, ...]
    history_turns: tuple[HistoryTurn, ...]
    anchor: Anchor | None
    legacy_anchor_content: AnchorContent | None
    anchor_error_code: str | None

## grade阶段产物 此时只差组装上下文 anchor已经生成完毕
@dataclass
class _GradeResult:
    """grade 阶段的内部计划，不暴露给 Precision 调用方。"""

    selected: list[ContextItem] # 选择的上下文
    lazyload: list[ContextItem] # P3级别上下文list
    dropped: list[ContextItem] # 丢弃的上下文 用于trace记录
    records: tuple[PrecisionRecord, ...] 
    anchor: Anchor | None # anchor 作为
    anchor_status: AnchorStatus
    anchor_builder_attempts: int
    anchor_error_code: str | None

## load - grade - build
class PrecisionBuilder:
    """上下文唯一入口；固定执行 load、grade、build 三个阶段。"""

    def __init__(
        self,
        *,
        trace: PrecisionTrace | None = None,
        anchor_builder: Callable[
            [Mapping[str, object]], Mapping[str, object]
        ] | None = None,
    ) -> None:
        """注入旁路 Trace 和 Anchor 模型 Adapter。"""
        self._trace = trace
        self._anchor_builder = anchor_builder

    def build(
        self,
        session: Session,
        *,
        memory_snapshot: MemorySnapshot | None = None,
        skill_selection: SkillSelection | None = None,
        ephemeral_prompt: str | None = None,
        budget: ContextBudget | None = None,
        token_estimator: TokenEstimator | None = None,
        trace_id: str = "context",
    ) -> PrecisionResult:
        """从所有固定来源构建一次可直接发送给模型的 messages。"""
        estimator = token_estimator or _ConservativeEstimator()
        ## 加载所有的context item
        loaded = self._load(
            session,
            memory_snapshot=memory_snapshot,
            skill_selection=skill_selection,
            ephemeral_prompt=ephemeral_prompt,
        )
        ## 按照规则分好级别
        graded = self._grade(loaded, budget=budget, estimator=estimator)
        ## 最终传入的信息（包含selected的部分 和 懒加载的部分）
        messages = self._build_messages(graded.selected, graded.lazyload)
        ## 最终token消耗
        final_cost = int(estimator.estimate_messages(messages))
        ## 预算约束
        if budget is not None and final_cost > budget.max_input_tokens:
            raise ContextBudgetExceededError()


        #### 以下均为指标计算 token 花费 percision相关指标统计等等
        selected_names = {item.name for item in graded.selected}
        p0_tokens = sum(
            item.token_estimate
            for item in graded.selected
            if item.priority == Priority.P0
        )
        anchor_tokens = next(
            (
                item.token_estimate
                for item in graded.selected
                if item.kind == "p2_anchor"
            ),
            0,
        )
        anchor_updated = graded.anchor_status in {"created", "extended"}
        metrics = PrecisionMetrics(
            trace_id=trace_id or "context",
            budget=budget.max_input_tokens if budget is not None else 0,
            tokens_used=final_cost,
            budget_usage=(final_cost / budget.max_input_tokens)
            if budget is not None and budget.max_input_tokens
            else 0.0,
            message_count=len(messages),
            total_chars=sum(len(json.dumps(m, ensure_ascii=False)) for m in messages),
            dropped_count=len(graded.dropped),
            deferred_count=len(graded.lazyload),
            compressed_count=sum(
                record.decision == "compressed" and record.item_name != "p2_anchor"
                for record in graded.records
            ),
            p0_dropped_count=sum(
                item.priority == Priority.P0 for item in graded.dropped
            ),
            p1_dropped_count=sum(
                item.priority == Priority.P1 for item in graded.dropped
            ),
            protected_error_missing_count=sum(
                item.is_error
                and item.priority in {Priority.P0, Priority.P1}
                and item.name not in selected_names
                for item in loaded.items
            ),
            p3_exposed_count=len(graded.lazyload),
            p0_over_share=bool(
                budget is not None
                and budget.max_input_tokens
                and p0_tokens / budget.max_input_tokens > _P0_SHARE_WARNING
            ),
            anchor_status=graded.anchor_status,
            anchor_covered_turns=(
                graded.anchor.covered_turn_count if graded.anchor is not None else 0
            ),
            anchor_tokens=anchor_tokens,
            anchor_builder_attempts=graded.anchor_builder_attempts,
            anchor_error_code=graded.anchor_error_code,
        )

        ## 记录落库的最终结果
        result = PrecisionResult(
            messages=messages,
            metrics=metrics,
            records=graded.records,
            exposed_refs=tuple(item.origin_ref or item.name for item in graded.lazyload),
            anchor=graded.anchor,
            anchor_updated=anchor_updated,
        )
        if self._trace is not None:
            self._trace.record(result)
        return result

    ## 加载无需历史数据（记忆 skill 经验） + 加载历史数据（session） + 加载anchor（校验后放入p2a） + 
    def _load(
        self,
        session: Session,
        *,
        memory_snapshot: MemorySnapshot | None,
        skill_selection: SkillSelection | None,
        ephemeral_prompt: str | None,
    ) -> _LoadedContext:
        """加载固定来源，校验 Anchor，并统一转换成 ContextItem。"""
        tagged = self._load_nonhistory(
            memory_snapshot=memory_snapshot,
            skill_selection=skill_selection,
            ephemeral_prompt=ephemeral_prompt,
        )
        nonhistory = _items_from_nonhistory(tagged)
        history = self._history_items(session.messages, len(nonhistory) + 1)
        history_turns = _history_turns(history)

        stored_anchor, anchor_error_code = _coerce_anchor(session.context_anchor)
        if stored_anchor is not None:
            anchor_error_code = _validate_stored_anchor(
                stored_anchor,
                session_id=session.session_id,
                turns=history_turns,
            )
            if anchor_error_code is not None:
                stored_anchor = None

        anchor_items: tuple[ContextItem, ...] = ()
        if stored_anchor is not None:
            anchor_items = (
                _context_item_from_anchor(
                    stored_anchor,
                    seq=_anchor_sequence(history_turns),
                ),
            )

        legacy_anchor_content = (
            None
            if stored_anchor is not None
            else _parse_legacy_anchor(session.summary or "")
        )
        return _LoadedContext(
            session_id=session.session_id,
            items=(*nonhistory, *anchor_items, *history),
            history_turns=history_turns,
            anchor=stored_anchor,
            legacy_anchor_content=legacy_anchor_content,
            anchor_error_code=anchor_error_code,
        )
    ## 情景记忆 + skill + 经验卡片
    def _load_nonhistory(
        self,
        *,
        memory_snapshot: MemorySnapshot | None,
        skill_selection: SkillSelection | None,
        ephemeral_prompt: str | None,
    ) -> list[tuple[str, dict]]:
        """按固定顺序加载系统、Memory、Skill 和临时提示。"""
        tagged: list[tuple[str, dict]] = []
        try:
            system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("system prompt 文件缺失: %s，使用兜底文案", SYSTEM_PROMPT_PATH)
            system_prompt = "你是一个友好的AI助手。"
        tagged.append(("system_prompt", {"role": "system", "content": system_prompt}))

        if memory_snapshot is not None:
            has_segments = (
                memory_snapshot.hot_prompt is not None
                or bool(memory_snapshot.daily_prompt)
                or bool(memory_snapshot.provider_prompt)
            )
            hot = (
                memory_snapshot.hot_prompt
                if has_segments and memory_snapshot.hot_prompt is not None
                else memory_snapshot.prompt
            )
            tagged.append(("memory_hot", {"role": "system", "content": hot}))
            if has_segments and memory_snapshot.daily_prompt:
                tagged.append((
                    "memory_daily",
                    {"role": "system", "content": memory_snapshot.daily_prompt},
                ))
            if has_segments and memory_snapshot.provider_prompt:
                tagged.append((
                    "memory_provider",
                    {"role": "system", "content": memory_snapshot.provider_prompt},
                ))

        if skill_selection is not None and skill_selection.summary_prompt:
            tagged.append((
                "skill",
                {"role": "system", "content": skill_selection.summary_prompt},
            ))
        if ephemeral_prompt:
            tagged.append(("ephemeral", {"role": "system", "content": ephemeral_prompt}))
        return tagged

    ## 每个历史会话messag转为原子记忆  最近的对话 p0  最近的agent执行or回复 p1 session存储的对话 p2
    def _history_items(
        self, messages: Sequence[Message], history_start_seq: int
    ) -> tuple[ContextItem, ...]:
        """保留 message_id/seq_id，把 Session 历史映射成可分级原子。"""
        last_user_position = next(
            (
                position
                for position in range(len(messages) - 1, -1, -1)
                if messages[position].role == "user"
            ),
            None,
        )
        items: list[ContextItem] = []
        for position, source in enumerate(messages):
            if last_user_position is not None and position == last_user_position:
                priority = Priority.P0
            elif last_user_position is not None and position > last_user_position:
                priority = Priority.P1
            else:
                priority = Priority.P2
            mapped = self._map_message(source)
            items.append(ContextItem(
                name=f"history:seq:{source.seq_id}",
                kind="history",
                priority=priority,
                message=mapped,
                is_error=_is_tool_error(mapped),
                seq=history_start_seq + position,
                message_id=source.message_id,
                session_seq=source.seq_id,
            ))
        return tuple(items)

    def _grade(
        self,
        loaded: _LoadedContext,
        *,
        budget: ContextBudget | None,
        estimator: TokenEstimator,
    ) -> _GradeResult:
        """执行固定优先级、Anchor 覆盖替换、窗口约束和失败关闭。"""
        estimated = tuple(_with_estimate(item, estimator) for item in loaded.items)
        item_by_name = {item.name: item for item in estimated}
        history_turns = tuple(
            tuple(item_by_name[item.name] for item in turn)
            for turn in loaded.history_turns
        )
        cap = budget.max_input_tokens if budget is not None else _UNLIMITED
        reasons: dict[str, tuple[Decision, str]] = {}
        dropped: list[ContextItem] = []
        record_items = list(estimated)

        mustload = [
            item for item in estimated if item.priority in {Priority.P0, Priority.P1}
        ]
        selected = list(mustload)
        must_tokens = sum(item.token_estimate for item in mustload)
        if must_tokens > cap:
            raise ContextBudgetExceededError()
        for item in mustload:
            reasons[item.name] = (
                "selected",
                "protected_error"
                if item.is_error
                else "keep_p0"
                if item.priority == Priority.P0
                else "keep_p1",
            )

        all_p2_turns = tuple(
            turn
            for turn in history_turns
            if turn and all(item.priority == Priority.P2 for item in turn)
        )
        covered_count = loaded.anchor.covered_turn_count if loaded.anchor else 0
        covered_turns = all_p2_turns[:covered_count]
        uncovered_turns = all_p2_turns[covered_count:]
        for turn in covered_turns:
            for item in turn:
                reasons[item.name] = ("compressed", "covered_by_anchor")

        current_anchor = loaded.anchor
        anchor_item = next(
            (item for item in estimated if item.kind == "p2_anchor"),
            None,
        )
        anchor_tokens = anchor_item.token_estimate if anchor_item is not None else 0
        if anchor_item is not None:
            selected.append(anchor_item)
            reasons[anchor_item.name] = ("selected", "p2_anchor_reused")
        if must_tokens + anchor_tokens > cap:
            raise ContextBudgetExceededError()

        raw_uncovered_cost = sum(_turn_cost(turn) for turn in uncovered_turns)
        raw_with_anchor_cost = must_tokens + anchor_tokens + raw_uncovered_cost
        anchor_limit = _anchor_token_limit(cap) if budget is not None else 0
        trigger = bool(
            budget is not None
            and cap
            and (
                raw_with_anchor_cost > cap
                or raw_with_anchor_cost / cap >= _COMPACT_RATIO
                or anchor_tokens > anchor_limit
            )
        )

        candidate_turns: tuple[HistoryTurn, ...] = ()
        needs_anchor_refresh = bool(
            budget is not None
            and current_anchor is not None
            and anchor_tokens > anchor_limit
        )
        if trigger and len(uncovered_turns) > _KEEP_RECENT_P2_TURNS:
            remaining = list(uncovered_turns)
            candidates: list[HistoryTurn] = []
            reserve = max(1, anchor_limit)
            if loaded.legacy_anchor_content is not None:
                candidates = remaining[:-_KEEP_RECENT_P2_TURNS]
                remaining = remaining[-_KEEP_RECENT_P2_TURNS:]
            else:
                while len(remaining) > _KEEP_RECENT_P2_TURNS:
                    projected = (
                        must_tokens
                        + reserve
                        + sum(_turn_cost(turn) for turn in remaining)
                    )
                    if projected / cap < _COMPACT_RATIO and projected <= cap:
                        break
                    candidates.append(remaining.pop(0))
            candidate_turns = tuple(candidates)

        if candidate_turns:
            recent_cost = sum(
                _turn_cost(turn)
                for turn in uncovered_turns[-_KEEP_RECENT_P2_TURNS:]
            )
            try:
                minimum_anchor_cost = int(estimator.estimate_messages([{
                    "role": "system", "content": ""
                }]))
            except Exception:
                minimum_anchor_cost = 1
            if must_tokens + recent_cost + minimum_anchor_cost > cap:
                raise ContextBudgetExceededError()

        attempts = 0
        anchor_error_code = loaded.anchor_error_code
        anchor_status: AnchorStatus = (
            "reused" if current_anchor is not None else "not_needed"
        )

        if candidate_turns or needs_anchor_refresh:
            if self._anchor_builder is None:
                anchor_error_code = "anchor_builder_unavailable"
                current_anchor, selected = self._raw_fallback(
                    mustload,
                    all_p2_turns,
                    cap,
                    current_anchor=current_anchor,
                    error_code=anchor_error_code,
                )
                anchor_status = "raw_fallback"
            else:
                coverage_turns = self._coverage_turns(
                    all_p2_turns,
                    covered_count,
                    candidate_turns,
                )
                built_anchor, built_item, attempts, build_error = self._try_build_anchor(
                    loaded,
                    coverage_turns=coverage_turns,
                    candidate_turns=candidate_turns,
                    max_tokens=anchor_limit,
                    estimator=estimator,
                )
                if built_anchor is None or built_item is None:
                    anchor_error_code = build_error or "anchor_output_invalid"
                    current_anchor, selected = self._raw_fallback(
                        mustload,
                        all_p2_turns,
                        cap,
                        current_anchor=current_anchor,
                        error_code=anchor_error_code,
                    )
                    anchor_status = "raw_fallback"
                else:
                    current_anchor = built_anchor
                    selected = [*mustload, built_item]
                    record_items = [
                        item for item in record_items if item.name != "p2_anchor"
                    ]
                    record_items.append(built_item)
                    reasons[built_item.name] = (
                        "selected",
                        "p2_anchor_extended"
                        if loaded.anchor is not None
                        else "p2_anchor_created",
                    )
                    covered_names = {
                        item.name for turn in coverage_turns for item in turn
                    }
                    for turn in all_p2_turns:
                        for item in turn:
                            if item.name in covered_names:
                                reasons[item.name] = (
                                    "compressed", "covered_by_anchor"
                                )
                            else:
                                selected.append(item)
                                reasons[item.name] = (
                                    "selected", "p2_recent_raw"
                                )
                    if sum(item.token_estimate for item in selected) > cap:
                        raise ContextBudgetExceededError()
                    anchor_status = (
                        "extended" if loaded.anchor is not None else "created"
                    )
                    anchor_error_code = loaded.anchor_error_code
        else:
            for turn in uncovered_turns:
                selected.extend(turn)
                for item in turn:
                    reasons[item.name] = ("selected", "p2_raw")
            if sum(item.token_estimate for item in selected) > cap:
                raise ContextBudgetExceededError()

        if anchor_status == "raw_fallback":
            record_items = [
                item for item in record_items if item.name != "p2_anchor"
            ]
            reasons.pop("p2_anchor", None)
            for turn in all_p2_turns:
                for item in turn:
                    reasons[item.name] = (
                        "selected", "anchor_failed_raw_fallback"
                    )

        selected_names = {item.name for item in selected}
        for turn in all_p2_turns:
            for item in turn:
                if item.name in selected_names:
                    continue
                if reasons.get(item.name, (None, None))[0] == "compressed":
                    continue
                selected.append(item)
                selected_names.add(item.name)
                reasons[item.name] = ("selected", "p2_raw")
        if sum(item.token_estimate for item in selected) > cap:
            raise ContextBudgetExceededError()

        lazy_candidates = [item for item in estimated if item.priority == Priority.P3]
        lazyload: list[ContextItem] = []
        if lazy_candidates:
            lazy_cost = int(estimator.estimate_messages([{
                "role": "system",
                "content": _lazy_context_block(lazy_candidates),
            }]))
            if sum(item.token_estimate for item in selected) + lazy_cost <= cap:
                lazyload = lazy_candidates
                for item in lazyload:
                    reasons[item.name] = ("lazyload", "p3_lazyload")
            else:
                dropped.extend(lazy_candidates)
                for item in lazy_candidates:
                    reasons[item.name] = (
                        "dropped", "p3_handle_over_budget"
                    )

        selected.sort(key=lambda item: item.seq)
        return _GradeResult(
            selected=selected,
            lazyload=lazyload,
            dropped=dropped,
            records=_make_records(record_items, reasons),
            anchor=current_anchor,
            anchor_status=anchor_status,
            anchor_builder_attempts=attempts,
            anchor_error_code=anchor_error_code,
        )

    def _coverage_turns(
        self,
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
        if not all(_turn_starts_with_user(turn) for turn in coverage):
            raise AnchorCompactionError(
                "anchor_protocol_invalid", "待压缩前缀存在没有 user 起点的历史轮次"
            )
        return coverage

    def _try_build_anchor(
        self,
        loaded: _LoadedContext,
        *,
        coverage_turns: Sequence[HistoryTurn],
        candidate_turns: Sequence[HistoryTurn],
        max_tokens: int,
        estimator: TokenEstimator,
    ) -> tuple[Anchor | None, ContextItem | None, int, str | None]:
        """最多调用 Builder 两次，并对结构、来源、错误和预算校验。"""
        allowed_ids = tuple(
            item.message_id or ""
            for turn in coverage_turns
            for item in turn
            if item.message_id
        )
        existing_content = (
            _anchor_content(loaded.anchor)
            if loaded.anchor is not None
            else loaded.legacy_anchor_content
        )
        candidate_start = len(coverage_turns) - len(candidate_turns)
        new_turns = [
            _anchor_turn_payload(turn, candidate_start + offset)
            for offset, turn in enumerate(candidate_turns)
        ]
        validation_error: str | None = loaded.anchor_error_code
        last_code: str | None = None

        for attempt in range(1, 3):
            target = max(1, max_tokens if attempt == 1 else max_tokens // 2)
            payload: dict[str, object] = {
                "existing_anchor": (
                    _anchor_content_payload(existing_content)
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
                raw_content = self._anchor_builder(payload)  # type: ignore[misc]
                content = _validate_anchor_content(
                    raw_content,
                    allowed_ids=set(allowed_ids),
                    coverage_turns=coverage_turns,
                )
                anchor = _attach_anchor_coverage(
                    content,
                    session_id=loaded.session_id,
                    coverage_turns=coverage_turns,
                )
                item = _with_estimate(
                    _context_item_from_anchor(
                        anchor,
                        seq=_anchor_sequence(loaded.history_turns),
                    ),
                    estimator,
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

    def _raw_fallback(
        self,
        mustload: Sequence[ContextItem],
        all_p2_turns: Sequence[HistoryTurn],
        cap: int,
        *,
        current_anchor: Anchor | None,
        error_code: str,
    ) -> tuple[Anchor | None, list[ContextItem]]:
        """Builder 失败时仅在全部原文可装入时安全降级。"""
        raw = [item for turn in all_p2_turns for item in turn]
        selected = [*mustload, *raw]
        if sum(item.token_estimate for item in selected) > cap:
            raise AnchorCompactionError(error_code)
        return current_anchor, selected

    @staticmethod
    def _build_messages(
        selected: Sequence[ContextItem], lazyload: Sequence[ContextItem]
    ) -> list[dict]:
        """按统一 seq 构建正文，P3 永远只追加 handle。"""
        messages = [
            copy.deepcopy(item.message)
            for item in sorted(selected, key=lambda item: item.seq)
        ]
        lazy = _lazy_context_block(lazyload)
        if lazy:
            messages.append({"role": "system", "content": lazy})
        return messages

    @staticmethod
    def _map_message(message: Message) -> dict:
        """把全局 Message 映射成模型接口的统一消息字典。"""
        if message.role == "assistant" and message.metadata.get("tool_calls"):
            return {
                "role": "assistant",
                "content": message.content_text or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call.get("arguments_raw") or "{}",
                        },
                    }
                    for call in message.metadata["tool_calls"]
                ],
            }
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.metadata.get("tool_call_id", ""),
                "content": message.content_text,
            }
        return {"role": message.role, "content": message.content_text}


def _items_from_nonhistory(
    tagged: Sequence[tuple[str, dict]],
) -> tuple[ContextItem, ...]:
    """把非历史来源转换成固定优先级原子。"""
    items: list[ContextItem] = []
    for seq, (kind, message) in enumerate(tagged):
        priority = (
            Priority.P0
            if kind in _P0_KINDS
            else Priority.P3
            if kind in _P3_KINDS
            else Priority.P2
        )
        items.append(ContextItem(
            name=kind,
            kind=kind,
            priority=priority,
            message=message,
            origin_ref=_origin_ref(kind, message),
            seq=seq,
        ))
    return tuple(items)


def _history_turns(items: Sequence[ContextItem]) -> tuple[HistoryTurn, ...]:
    """以 user 为起点切分历史，保持 assistant/tool 协议不可拆。"""
    turns: list[HistoryTurn] = []
    current: list[ContextItem] = []
    for item in sorted(items, key=lambda candidate: candidate.seq):
        if item.message.get("role") == "user" and current:
            turns.append(tuple(current))
            current = [item]
        else:
            current.append(item)
    if current:
        turns.append(tuple(current))
    return tuple(turns)


def _turn_cost(turn: HistoryTurn) -> int:
    """返回一个完整历史轮次的估算成本。"""
    return sum(item.token_estimate for item in turn)


def _turn_starts_with_user(turn: HistoryTurn) -> bool:
    """返回历史轮次是否具有可信 user 起点。"""
    return bool(turn and turn[0].message.get("role") == "user")


def _coerce_anchor(value: Anchor | dict | None) -> tuple[Anchor | None, str | None]:
    """恢复全局 Anchor；损坏字典不阻断 Session 原文恢复。"""
    if value in (None, "", {}):
        return None, None
    if isinstance(value, Anchor):
        return value, None
    try:
        return Anchor.model_validate(value), None
    except (TypeError, ValueError, ValidationError):
        return None, "anchor_payload_invalid"


def _validate_stored_anchor(
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
        or not all(_turn_starts_with_user(turn) for turn in coverage)
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
    digest = _source_digest(coverage)
    if anchor.source_digest != digest:
        return "anchor_digest_mismatch"
    if anchor.anchor_id != _anchor_id(session_id, last.message_id or "", digest):
        return "anchor_id_invalid"
    allowed = {
        item.message_id for turn in coverage for item in turn if item.message_id
    }
    for fact in _anchor_facts(anchor):
        if not fact.source_message_ids or not set(fact.source_message_ids) <= allowed:
            return "anchor_source_invalid"
    return None


def _validate_anchor_content(
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


def _attach_anchor_coverage(
    content: AnchorContent,
    *,
    session_id: str,
    coverage_turns: Sequence[HistoryTurn],
) -> Anchor:
    """由运行时给模型语义附加不可伪造的连续覆盖账本。"""
    first = coverage_turns[0][0]
    last = coverage_turns[-1][-1]
    digest = _source_digest(coverage_turns)
    return Anchor(
        anchor_id=_anchor_id(session_id, last.message_id or "", digest),
        session_id=session_id,
        covered_start_seq=int(first.session_seq or 0),
        covered_end_seq=int(last.session_seq or 0),
        covered_start_message_id=first.message_id or "",
        covered_end_message_id=last.message_id or "",
        covered_turn_count=len(coverage_turns),
        source_digest=digest,
        **content,
    )


def _anchor_content(anchor: Anchor) -> AnchorContent:
    """返回不含覆盖账本的五槽内容。"""
    return {
        "intent": anchor.intent,
        "changes": anchor.changes,
        "decisions": anchor.decisions,
        "next_steps": anchor.next_steps,
        "excluded": anchor.excluded,
    }


def _anchor_content_payload(content: AnchorContent) -> dict[str, list[dict]]:
    """把五槽内容序列化成 Builder 可消费的 JSON 对象。"""
    return {
        slot: [fact.model_dump(mode="json") for fact in content[slot]]
        for slot in _ANCHOR_SLOTS
    }


def _anchor_facts(anchor: Anchor) -> tuple[AnchorFact, ...]:
    """按固定槽顺序返回 Anchor 全部事实。"""
    return (
        *anchor.intent,
        *anchor.changes,
        *anchor.decisions,
        *anchor.next_steps,
        *anchor.excluded,
    )


def _parse_legacy_anchor(text: str) -> AnchorContent | None:
    """读取旧五槽文本作为迁移提示，但不建立覆盖关系。"""
    if "<context-anchor>" not in (text or ""):
        return None
    title_to_slot = {
        "Intent": "intent",
        "Changes": "changes",
        "Decisions": "decisions",
        "Next steps": "next_steps",
        "Excluded": "excluded",
    }
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


def _anchor_turn_payload(turn: HistoryTurn, turn_index: int) -> dict:
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


def _source_digest(turns: Sequence[HistoryTurn]) -> str:
    """对覆盖原文的身份、顺序和模型可见内容计算稳定摘要。"""
    payload = [
        {
            "message_id": item.message_id,
            "seq_id": item.session_seq,
            "message": item.message,
        }
        for turn in turns
        for item in turn
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _anchor_id(session_id: str, end_message_id: str, digest: str) -> str:
    """根据 Session、覆盖边界和摘要生成确定性 Anchor 标识。"""
    raw = f"{session_id}:{end_message_id}:{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _anchor_sequence(turns: Sequence[HistoryTurn]) -> int:
    """返回 Anchor 在统一上下文中位于所有 Session 历史之前的位置。"""
    if not turns or not turns[0]:
        return 0
    return turns[0][0].seq - 1


def _context_item_from_anchor(anchor: Anchor, *, seq: int) -> ContextItem:
    """把全局 Anchor 转成优先于历史原文的 P2-A 原子。"""
    return ContextItem(
        name="p2_anchor",
        kind="p2_anchor",
        priority=Priority.P2,
        message={"role": "system", "content": render_anchor(anchor)},
        seq=seq,
    )


def _anchor_token_limit(cap: int) -> int:
    """返回 Anchor 的 15% 动态上限和 2048 token 硬上限。"""
    return max(1, min(int(cap * _ANCHOR_BUDGET_RATIO), _ANCHOR_HARD_TOKEN_CAP))


def _origin_ref(kind: str, message: dict) -> str | None:
    """从 Daily/Provider 围栏中提取可暴露给模型的稳定 handle。"""
    content = str(message.get("content") or "")
    if kind == "memory_daily":
        match = re.search(r'date="([^"]+)"', content)
        return f"daily:{match.group(1)}" if match else "daily"
    if kind == "memory_provider":
        match = re.search(r'name="([^"]+)"', content)
        return f"provider:{match.group(1)}" if match else "provider"
    return None


def _is_tool_error(message: dict) -> bool:
    """识别需要在 Anchor decisions 中显式保留的工具错误。"""
    return bool(
        message.get("role") == "tool"
        and str(message.get("content") or "").startswith(_TOOL_FAIL_PREFIX)
    )


def _with_estimate(item: ContextItem, estimator: TokenEstimator) -> ContextItem:
    """为尚未估算的 ContextItem 计算成本，异常时使用字符兜底。"""
    if item.token_estimate > 0:
        return item
    try:
        cost = int(estimator.estimate_messages([item.message]))
    except Exception:
        content = str(item.message.get("content") or "")
        cost = max(1, len(content) // 4)
    return item.model_copy(update={"token_estimate": max(0, cost)})


def _make_records(
    items: Sequence[ContextItem], reasons: dict[str, tuple[Decision, str]]
) -> tuple[PrecisionRecord, ...]:
    """把内部原子投影成稳定、可检查的逐条 Trace。"""
    unique = {item.name: item for item in items}
    return tuple(
        PrecisionRecord(
            item_name=item.name,
            priority=item.priority,
            token_estimate=item.token_estimate,
            decision=reasons.get(item.name, ("dropped", "unclassified"))[0],
            reason=reasons.get(item.name, ("dropped", "unclassified"))[1],
            kind=item.kind,
            role=str((item.message or {}).get("role", "")),
            content_preview=_trace_content(_message_trace_content(item.message)),
            content_chars=len(_message_trace_content(item.message)),
            origin_ref=item.origin_ref,
            seq=item.seq,
        )
        for item in sorted(unique.values(), key=lambda candidate: candidate.seq)
    )


def _trace_content(content: object, limit: int = 3000) -> str:
    """限制 Trace 正文大小，同时保留足够诊断信息。"""
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _message_trace_content(message: dict) -> str:
    """把普通正文和 tool_calls 消息渲染成 Trace 可读文本。"""
    content = message.get("content")
    if content:
        return str(content)
    if message.get("tool_calls"):
        return json.dumps(message, ensure_ascii=False)
    return ""


def _lazy_context_block(items: Sequence[ContextItem]) -> str:
    """把 P3 项构建成只含 handle、不含正文的 lazy-context 消息。"""
    if not items:
        return ""
    handles = "\n".join(f"- {item.origin_ref or item.name}" for item in items)
    return (
        "<lazy-context>\n"
        f"{handles}\n"
        "用 memory_read 按 handle 读取全文，正文不在上下文中。\n"
        "</lazy-context>"
    )
