"""Precision 的旁路观测；同一 trace_id 的多次 ReAct 构建只算一轮。"""
from __future__ import annotations

import math
import copy
import threading
from collections import deque


from agent.types.precision.result import PrecisionMetrics, PrecisionRecord, PrecisionResult

from agent.types.precision.trace import PrecisionBuildSnapshot, PrecisionTraceSnapshot, PrecisionTurnTrace




class PrecisionTrace:
    """最多保留最近 N 个真实 turn；只观察，不参与 Precision 决策。"""

    def __init__(self, window: int = 100):
        """创建线程安全的进程内滚动窗口。"""
        self._lock = threading.Lock()
        self._turns: deque[PrecisionTurnTrace] = deque(maxlen=window)

    def record(self, result: PrecisionResult, *, iteration: int | None = None) -> PrecisionTurnTrace:
        """保存完整构建，窗口仍按真实 turn 计数。"""
        with self._lock:
            previous = None
            for index, turn in enumerate(self._turns):
                if turn.trace_id == result.metrics.trace_id:
                    previous = turn
                    del self._turns[index]
                    break
            builds = previous.builds if previous else ()
            iteration = iteration if iteration is not None else max((b.iteration for b in builds), default=0) + 1
            build = PrecisionBuildSnapshot(iteration=iteration, messages=result.messages,
                records=result.records, metrics=result.metrics).model_copy(deep=True)
            builds = tuple(b for b in builds if b.iteration != iteration) + (build,)
            hits = tuple(ref for ref in previous.p3_hit_refs if ref in result.exposed_refs) if previous else ()
            turn = PrecisionTurnTrace(trace_id=result.metrics.trace_id, metrics=result.metrics,
                records=result.records, exposed_refs=result.exposed_refs, p3_hit_refs=hits,
                builds=tuple(sorted(builds, key=lambda b: b.iteration))).model_copy(deep=True)
            self._turns.append(turn)
            return turn.model_copy(deep=True)

    def record_request(self, trace_id: str, iteration: int, tools: list[dict]) -> None:
        """调用模型前绑定实际工具定义；重试复用快照。"""
        with self._lock:
            for index, turn in enumerate(self._turns):
                if turn.trace_id == trace_id:
                    builds = tuple(b.model_copy(update={"tools": copy.deepcopy(tools)}, deep=True)
                                   if b.iteration == iteration else b for b in turn.builds)
                    self._turns[index] = turn.model_copy(update={"builds": builds}, deep=True)
                    return

    def record_failure(self, trace_id: str, iteration: int, error: str) -> None:
        """失败迭代没有全文，不能使用上一迭代的输出冒充。"""
        with self._lock:
            previous = None
            for index, turn in enumerate(self._turns):
                if turn.trace_id == trace_id:
                    previous = turn
                    del self._turns[index]
                    break
            turn = previous or PrecisionTurnTrace(trace_id=trace_id)
            builds = tuple(b for b in turn.builds if b.iteration != iteration)
            failed = PrecisionBuildSnapshot(iteration=iteration, error=error)
            self._turns.append(turn.model_copy(update={"builds": tuple(sorted((*builds, failed), key=lambda b: b.iteration))}))

    def record_memory_read(self, trace_id: str, reference: str) -> tuple[str, ...]:
        """把一次成功 memory_read 对应到本轮真正暴露过的 P3 handle。"""
        with self._lock:
            for index in range(len(self._turns) - 1, -1, -1):
                turn = self._turns[index]
                if turn.trace_id != trace_id:
                    continue
                hits = _memory_read_hits(reference, set(turn.exposed_refs))
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
        metrics = [turn.metrics for turn in turns if turn.metrics is not None]
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

    def latest(self) -> PrecisionTurnTrace | None:
        """返回最近一次真实 turn 的 Trace；窗口为空时返回 None。"""
        with self._lock:
            return self._turns[-1].model_copy(deep=True) if self._turns else None

    def get(self, trace_id: str) -> PrecisionTurnTrace | None:
        """按真实 turn id 读取窗口内的最新 Trace。"""
        with self._lock:
            for turn in reversed(self._turns):
                if turn.trace_id == trace_id:
                    return turn.model_copy(deep=True)
        return None


def _memory_read_hits(reference: str, exposed: set[str]) -> set[str]:
    """Only an explicitly read reference that was exposed lazily counts as a hit."""
    return {reference} if isinstance(reference, str) and reference in exposed else set()


def _percentile(values: list[float], q: float) -> float:
    """使用 nearest-rank 规则计算小窗口也稳定的经验分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(math.ceil(q / 100.0 * len(ordered))))
    return float(ordered[rank - 1])
