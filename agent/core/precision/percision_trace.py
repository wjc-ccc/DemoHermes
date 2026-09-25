"""Precision 的旁路观测；同一 trace_id 的多次 ReAct 构建只算一轮。"""
from __future__ import annotations

import math
import threading
from collections import deque

from pydantic import BaseModel

from .percision_types import PrecisionMetrics, PrecisionRecord, PrecisionResult


class _TraceTurn(BaseModel):
    """一个真实 turn 的最新 Precision 结果及其 P3 命中状态。"""

    metrics: PrecisionMetrics
    records: tuple[PrecisionRecord, ...] = ()
    exposed_refs: tuple[str, ...] = ()
    p3_hit_refs: tuple[str, ...] = ()

    @property
    def trace_id(self) -> str:
        """透传聚合指标中的 turn 标识，供窗口内查找和覆盖更新。"""
        return self.metrics.trace_id


class PrecisionTraceSnapshot(BaseModel):
    """滚动窗口的长尾压力、关键层丢失和 P3 回取率。"""

    n: int
    budget_usage_p95: float
    budget_usage_p99: float
    dropped_count_p95: float
    dropped_count_p99: float
    deferred_count_p95: float
    deferred_count_p99: float
    p0_dropped_count: int
    p1_dropped_count: int
    protected_error_missing_count: int
    p3_hit_rate: float


class PrecisionTrace:
    """最多保留最近 N 个真实 turn；只观察，不参与 Precision 决策。"""

    def __init__(self, window: int = 100):
        """创建线程安全的进程内滚动窗口。"""
        self._lock = threading.Lock()
        self._turns: deque[_TraceTurn] = deque(maxlen=window)

    def record(self, result: PrecisionResult) -> _TraceTurn:
        """写入或更新同一 turn 的最新构建结果，并保留已经发生的 P3 hit。"""
        with self._lock:
            previous_hits: tuple[str, ...] = ()
            for index in range(len(self._turns) - 1, -1, -1):
                previous = self._turns[index]
                if previous.trace_id != result.metrics.trace_id:
                    continue
                previous_hits = tuple(
                    ref for ref in previous.p3_hit_refs if ref in result.exposed_refs
                )
                del self._turns[index]
                break
            turn = _TraceTurn(
                metrics=result.metrics,
                records=result.records,
                exposed_refs=result.exposed_refs,
                p3_hit_refs=previous_hits,
            )
            self._turns.append(turn)
            return turn

    def record_memory_read(self, trace_id: str, arguments: dict) -> tuple[str, ...]:
        """把一次成功 memory_read 对应到本轮真正暴露过的 P3 handle。"""
        with self._lock:
            for index in range(len(self._turns) - 1, -1, -1):
                turn = self._turns[index]
                if turn.trace_id != trace_id:
                    continue
                hits = _memory_read_hits(arguments, set(turn.exposed_refs))
                if not hits:
                    return ()
                merged = tuple(dict.fromkeys((*turn.p3_hit_refs, *sorted(hits))))
                self._turns[index] = turn.model_copy(update={"p3_hit_refs": merged})
                return tuple(sorted(hits))
        return ()

    def record_p3_hit(self, trace_id: str, origin_ref: str) -> None:
        """显式记录命中；仅接受本轮实际暴露的 handle。"""
        if not origin_ref:
            return
        with self._lock:
            for index in range(len(self._turns) - 1, -1, -1):
                turn = self._turns[index]
                if turn.trace_id != trace_id or origin_ref not in turn.exposed_refs:
                    continue
                if origin_ref in turn.p3_hit_refs:
                    return
                self._turns[index] = turn.model_copy(update={
                    "p3_hit_refs": (*turn.p3_hit_refs, origin_ref)
                })
                return

    def snapshot(self) -> PrecisionTraceSnapshot:
        """基于当前窗口计算 p95/p99、关键层丢失总数和 P3 命中率。"""
        with self._lock:
            turns = list(self._turns)
        if not turns:
            return PrecisionTraceSnapshot(
                n=0,
                budget_usage_p95=0.0,
                budget_usage_p99=0.0,
                dropped_count_p95=0.0,
                dropped_count_p99=0.0,
                deferred_count_p95=0.0,
                deferred_count_p99=0.0,
                p0_dropped_count=0,
                p1_dropped_count=0,
                protected_error_missing_count=0,
                p3_hit_rate=0.0,
            )
        metrics = [turn.metrics for turn in turns]
        exposed = sum(len(turn.exposed_refs) for turn in turns)
        hits = sum(len(turn.p3_hit_refs) for turn in turns)
        return PrecisionTraceSnapshot(
            n=len(turns),
            budget_usage_p95=_percentile([m.budget_usage for m in metrics], 95),
            budget_usage_p99=_percentile([m.budget_usage for m in metrics], 99),
            dropped_count_p95=_percentile([float(m.dropped_count) for m in metrics], 95),
            dropped_count_p99=_percentile([float(m.dropped_count) for m in metrics], 99),
            deferred_count_p95=_percentile([float(m.deferred_count) for m in metrics], 95),
            deferred_count_p99=_percentile([float(m.deferred_count) for m in metrics], 99),
            p0_dropped_count=sum(m.p0_dropped_count for m in metrics),
            p1_dropped_count=sum(m.p1_dropped_count for m in metrics),
            protected_error_missing_count=sum(
                m.protected_error_missing_count for m in metrics
            ),
            p3_hit_rate=(hits / exposed) if exposed else 0.0,
        )

    def latest(self) -> _TraceTurn | None:
        """返回最近一次真实 turn 的 Trace；窗口为空时返回 None。"""
        with self._lock:
            return self._turns[-1] if self._turns else None

    def get(self, trace_id: str) -> _TraceTurn | None:
        """按真实 turn id 读取窗口内的最新 Trace。"""
        with self._lock:
            for turn in reversed(self._turns):
                if turn.trace_id == trace_id:
                    return turn
        return None


def _memory_read_hits(arguments: dict, exposed: set[str]) -> set[str]:
    """判断一次 memory_read 参数实际命中了哪些已暴露 P3 handle。"""
    hits: set[str] = set()
    target = arguments.get("target")
    date = arguments.get("date")
    query = str(arguments.get("query") or "")
    for ref in exposed:
        if ref.startswith("daily:"):
            ref_date = ref.split(":", 1)[1]
            if target == "daily" and (not date or date == ref_date):
                hits.add(ref)
        elif ref.startswith("provider:") and query:
            provider_name = ref.split(":", 1)[1]
            if provider_name in query:
                hits.add(ref)
    return hits


def _percentile(values: list[float], q: float) -> float:
    """使用 nearest-rank 规则计算小窗口也稳定的经验分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(math.ceil(q / 100.0 * len(ordered))))
    return float(ordered[rank - 1])


default_precision_trace = PrecisionTrace()
