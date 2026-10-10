"""固定 Precision 主链：加载 → 分级 → 构建。"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from agent.prompt.prompt_loader import load_system_prompt
from agent.types.context import (
    ContextBudget,
    TokenEstimator,
    Anchor,
)
from agent.types.conversation import Message, Session
from .percision_admission import AdmissionPlan, admit
from . import percision_anchor as _anchor
from agent.types.precision.admission import AdmissionAction, AdmissionPolicy
from agent.core.precision.errors import AnchorCompactionError, ContextBudgetExceededError
from agent.types.precision.result import (
    AnchorStatus,
    Decision,
    PrecisionMetrics,
    PrecisionRecord,
    PrecisionResult,
    PrecisionConfig,
)
from agent.core.precision.context_item import ContextItem, HistoryTurn, _LoadedContext
from agent.types.precision.priority import Priority

if TYPE_CHECKING:
    from agent.types.memory.snapshot import MemorySnapshot
    from agent.types.skills import SkillSelection
    from agent.types.precision.judge import ContextJudge
    from .percision_trace import PrecisionTrace

logger = logging.getLogger(__name__)

## 固定对应的权限级别
_P0_KINDS = frozenset({"system_prompt", "skill", "ephemeral", "memory_manual"})
_P1_KINDS = frozenset({"memory_experience"})
_TOOL_FAIL_PREFIX = "工具执行失败"
_KEEP_RECENT_P2_TURNS = 2
_COMPACT_RATIO = 0.85
_P0_SHARE_WARNING = 0.10
_UNLIMITED = 2**31 - 1
_MESSAGE_OVERHEAD_BYTES = 16


class _ConservativeEstimator:
    """没有模型 tokenizer 时按 UTF-8 bytes 保守估算。"""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        """返回整批消息的保守字节成本，并计入协议开销。"""
        return sum(
            len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            + _MESSAGE_OVERHEAD_BYTES
            + 8192 * len(message.get("_images", []))
            for message in messages
        )

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
    admission: AdmissionPlan

## load - grade - build
@dataclass(frozen=True)
class _ProtectedContext:
    estimated: tuple[ContextItem, ...]
    cap: int
    mustload: tuple[ContextItem, ...]
    must_tokens: int
    selected: tuple[ContextItem, ...]
    reasons: dict[str, tuple[Decision, str]]
    turns: tuple[HistoryTurn, ...]
    covered_count: int
    anchor_tokens: int
    anchor: Anchor | None


@dataclass(frozen=True)
class _AdmittedHistory:
    protected: _ProtectedContext
    turns: tuple[HistoryTurn, ...]
    record_items: tuple[ContextItem, ...]
    admission: AdmissionPlan


@dataclass(frozen=True)
class _AnchorPlan:
    candidate_turns: tuple[HistoryTurn, ...]
    refresh: bool
    token_limit: int


@dataclass(frozen=True)
class _AnchorSelection:
    selected: tuple[ContextItem, ...]
    record_items: tuple[ContextItem, ...]
    reasons: dict[str, tuple[Decision, str]]
    anchor: Anchor | None
    status: AnchorStatus
    attempts: int
    error_code: str | None


class PrecisionBuilder:
    """上下文唯一入口；固定执行 load、grade、build 三个阶段。"""
    ## percision才进行对应的
    def __init__(
        self,
        *,
        trace: PrecisionTrace | None = None,
        anchor_builder: Callable[
            [Mapping[str, object]], Mapping[str, object]
        ] | None = None,
        context_judge: ContextJudge | None = None,
        admission_policy: AdmissionPolicy | None = None,
        config: PrecisionConfig | None = None,
    ) -> None:
        """注入旁路 Trace、Anchor 模型 Adapter 与准入判官。"""
        self._trace = trace
        self._anchor_builder = anchor_builder
        self._context_judge = context_judge
        self._admission_policy = admission_policy or AdmissionPolicy()
        self.config = config or PrecisionConfig()

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
        iteration: int | None = None,
    ) -> PrecisionResult:
        """从所有固定来源构建一次可直接发送给模型的 messages。"""
        budget = budget if budget is not None else self.config.budget
        estimator = token_estimator or self.config.token_estimator or _ConservativeEstimator()
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
        anchor_updated = graded.anchor_status in {"created", "extended"}
        admission = graded.admission
        metrics = self._measure(loaded, graded, messages, final_cost, budget, trace_id)

        ## 记录落库的最终结果
        result = PrecisionResult(
            messages=messages,
            metrics=metrics,
            records=graded.records,
            exposed_refs=tuple(item.origin_ref or item.name for item in graded.lazyload),
            anchor=graded.anchor,
            anchor_updated=anchor_updated,
            verdicts=admission.verdicts,
            verdicts_updated=admission.updated,
        )
        if self._trace is not None:
            self._trace.record(result, iteration=iteration)
        return result

    def _measure(self, loaded, graded, messages, final_cost, budget, trace_id) -> PrecisionMetrics:
        """唯一指标计算位置，只读取阶段结果。"""
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
        admission = graded.admission
        return PrecisionMetrics(
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
            admission_mode=self._admission_policy.mode,
            admission_candidates=admission.candidates,
            admission_judged_count=admission.judged,
            admission_judge_failures=admission.judge_failures,
            admission_judge_latency_ms=admission.judge_latency_ms,
            admission_rule_count=admission.rule_count,
            admission_trimmed_count=admission.trimmed,
            admission_deferred_count=admission.deferred,
            admission_saved_tokens=admission.saved_tokens,
            admission_missed_count=admission.missed,
        )


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

        stored_anchor, anchor_error_code = _anchor.coerce_anchor(session.context_anchor)
        if stored_anchor is not None:
            anchor_error_code = _anchor.validate_stored_anchor(
                stored_anchor,
                session_id=session.session_id,
                turns=history_turns,
            )
            if anchor_error_code is not None:
                stored_anchor = None

        anchor_items: tuple[ContextItem, ...] = ()
        if stored_anchor is not None:
            anchor_items = (
                _anchor.context_item_from_anchor(
                    stored_anchor,
                    seq=_anchor.anchor_sequence(history_turns),
                ),
            )

        legacy_anchor_content = (
            None
            if stored_anchor is not None
            else _anchor.parse_legacy_anchor(session.summary or "")
        )
        return _LoadedContext(
            session_id=session.session_id,
            items=(*nonhistory, *anchor_items, *history),
            history_turns=history_turns,
            anchor=stored_anchor,
            legacy_anchor_content=legacy_anchor_content,
            anchor_error_code=anchor_error_code,
            verdicts=dict(getattr(session, "context_verdicts", None) or {}),
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
        system_prompt = load_system_prompt()
        tagged.append(("system_prompt", {"role": "system", "content": system_prompt}))

        if memory_snapshot is not None:
            tagged.append(("memory_manual", {"role": "system", "content": memory_snapshot.prompt}))
            if memory_snapshot.experience_prompt:
                tagged.append((
                    "memory_experience",
                    {"role": "system", "content": memory_snapshot.experience_prompt},
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
            if source.status == "failed" and source.role != "user":
                continue
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

    def _grade(self, loaded, *, budget, estimator) -> _GradeResult:
        """阶段输入输出固定；预算与历史取舍继续沿用原规则。"""
        protected = self._protect_context(loaded, budget=budget, estimator=estimator)
        admitted = self._admit_history(loaded, protected, budget=budget, estimator=estimator)
        plan = self._plan_anchor(loaded, admitted, budget=budget, estimator=estimator)
        selected = self._select_history(loaded, admitted, plan, estimator=estimator)
        return self._finish_grade(admitted, selected, estimator=estimator)

    def _protect_context(self, loaded, *, budget, estimator) -> _ProtectedContext:
        estimated = tuple(_with_estimate(item, estimator) for item in loaded.items)
        item_by_name = {item.name: item for item in estimated}
        history_turns = tuple(
            tuple(item_by_name[item.name] for item in turn)
            for turn in loaded.history_turns
        )
        cap = budget.max_input_tokens if budget is not None else _UNLIMITED
        reasons: dict[str, tuple[Decision, str]] = {}

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

        return _ProtectedContext(
            estimated, cap, tuple(mustload), must_tokens, tuple(selected), reasons,
            all_p2_turns, covered_count, anchor_tokens, current_anchor,
        )

    def _admit_history(self, loaded, protected, *, budget, estimator) -> _AdmittedHistory:
        estimated, cap = protected.estimated, protected.cap
        must_tokens, anchor_tokens = protected.must_tokens, protected.anchor_tokens
        all_p2_turns, covered_count = protected.turns, protected.covered_count
        uncovered_turns = all_p2_turns[covered_count:]
        record_items = list(estimated)
        # 第一级：条目级准入（可读回）；之后仍超阈值才由 Anchor 做整轮语义压缩。
        admission = self._admit(
            loaded,
            all_p2_turns,
            covered_count=covered_count,
            live_items=[
                item
                for item in estimated
                if item.kind == "history"
                and item.priority in {Priority.P0, Priority.P1}
            ],
            usage=(
                (
                    must_tokens
                    + anchor_tokens
                    + sum(_turn_cost(turn) for turn in uncovered_turns)
                ) / cap
                if budget is not None and cap
                else 0.0
            ),
            estimator=estimator,
        )
        if admission.projected:
            all_p2_turns = tuple(
                tuple(admission.projected.get(item.name, item) for item in turn)
                for turn in all_p2_turns
            )
            uncovered_turns = all_p2_turns[covered_count:]
            record_items = [
                admission.projected.get(item.name, item) for item in record_items
            ]

        return _AdmittedHistory(protected, all_p2_turns, tuple(record_items), admission)

    def _plan_anchor(self, loaded, admitted, *, budget, estimator) -> _AnchorPlan:
        protected = admitted.protected
        cap, must_tokens = protected.cap, protected.must_tokens
        anchor_tokens, current_anchor = protected.anchor_tokens, protected.anchor
        uncovered_turns = admitted.turns[protected.covered_count:]
        raw_uncovered_cost = sum(_turn_cost(turn) for turn in uncovered_turns)
        raw_with_anchor_cost = must_tokens + anchor_tokens + raw_uncovered_cost
        anchor_limit = _anchor.anchor_token_limit(cap) if budget is not None else 0
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

        return _AnchorPlan(candidate_turns, needs_anchor_refresh, anchor_limit)

    def _select_history(self, loaded, admitted, plan, *, estimator) -> _AnchorSelection:
        protected = admitted.protected
        cap, mustload = protected.cap, protected.mustload
        selected, reasons = list(protected.selected), dict(protected.reasons)
        record_items = list(admitted.record_items)
        all_p2_turns, covered_count = admitted.turns, protected.covered_count
        uncovered_turns = all_p2_turns[covered_count:]
        current_anchor = protected.anchor
        candidate_turns, needs_anchor_refresh, anchor_limit = plan.candidate_turns, plan.refresh, plan.token_limit
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
                coverage_turns = _anchor.coverage_turns(
                    all_p2_turns,
                    covered_count,
                    candidate_turns,
                )
                built_anchor, built_item, attempts, build_error = _anchor.try_build_anchor(
                    loaded,
                    builder=self._anchor_builder,
                    coverage_turns=coverage_turns,
                    candidate_turns=candidate_turns,
                    max_tokens=anchor_limit,
                    estimate_item=lambda item: _with_estimate(item, estimator),
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

        return _AnchorSelection(tuple(selected), tuple(record_items), reasons,
                                current_anchor, anchor_status, attempts, anchor_error_code)

    def _finish_grade(self, admitted, selection, *, estimator) -> _GradeResult:
        estimated, cap = admitted.protected.estimated, admitted.protected.cap
        selected, reasons = list(selection.selected), dict(selection.reasons)
        record_items, admission = selection.record_items, admitted.admission
        current_anchor, anchor_status = selection.anchor, selection.status
        attempts, anchor_error_code = selection.attempts, selection.error_code
        dropped: list[ContextItem] = []
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
            records=_make_records(record_items, reasons, admission.notes),
            anchor=current_anchor,
            anchor_status=anchor_status,
            anchor_builder_attempts=attempts,
            anchor_error_code=anchor_error_code,
            admission=admission,
        )

    def _admit(
        self,
        loaded: _LoadedContext,
        all_p2_turns: Sequence[HistoryTurn],
        *,
        covered_count: int,
        live_items: Sequence[ContextItem],
        usage: float,
        estimator: TokenEstimator,
    ) -> AdmissionPlan:
        """对未被 Anchor 覆盖的 P2 工具结果执行准入；off 时只透传已有定案。"""

        def estimate(message: dict) -> int:
            try:
                return int(estimator.estimate_messages([message]))
            except Exception:
                return max(1, len(str(message.get("content") or "")) // 4)

        return admit(
            all_p2_turns,
            covered_count=covered_count,
            live_items=live_items,
            usage=usage,
            policy=self._admission_policy,
            judge=self._context_judge,
            stored=loaded.verdicts,
            estimate=estimate,
        )

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
        mapped = {"role": message.role, "content": message.content_text}
        if message.role == "user":
            images = [
                {"path": part.path, "mime_type": part.mime_type, "file_name": part.file_name}
                for part in message.content
                if part.type == "image" and part.path
            ]
            if images:
                mapped["_images"] = images
        return mapped


def _items_from_nonhistory(
    tagged: Sequence[tuple[str, dict]],
) -> tuple[ContextItem, ...]:
    """把非历史来源转换成固定优先级原子。"""
    items: list[ContextItem] = []
    for seq, (kind, message) in enumerate(tagged):
        priority = (
            Priority.P0
            if kind in _P0_KINDS
            else Priority.P1
            if kind in _P1_KINDS
            else Priority.P2
        )
        items.append(ContextItem(
            name=kind,
            kind=kind,
            priority=priority,
            message=message,
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
    items: Sequence[ContextItem],
    reasons: dict[str, tuple[Decision, str]],
    admission_notes: Mapping[str, tuple[AdmissionAction, str]],
) -> tuple[PrecisionRecord, ...]:
    """把内部原子投影成稳定、可检查的逐条 Trace。"""
    unique = {item.name: item for item in items}
    return tuple(
        PrecisionRecord(
            admission_action=admission_notes.get(item.name, (None, ""))[0],
            admission_reason=admission_notes.get(item.name, (None, ""))[1],
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
