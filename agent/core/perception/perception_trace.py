"""Own complete pending turns; keep a bounded window for completed statistics."""
from __future__ import annotations
import copy
import math
import threading
from collections import deque
from agent.types.perception.result import PerceptionMetrics, PerceptionResult
from agent.types.perception.trace import ModelCallSnapshot, PerceptionBuildSnapshot, PerceptionTraceSnapshot, PerceptionTurnTrace


class PerceptionTrace:
    def __init__(self, window: int = 100):
        if window < 1:
            raise ValueError("trace window must be positive")
        self._lock = threading.RLock()
        self._window: deque[str] = deque(maxlen=window)
        self._pending: dict[str, PerceptionTurnTrace] = {}
        self._completed: dict[str, PerceptionTurnTrace] = {}

    def _find(self, trace_id):
        return self._pending.get(trace_id) or self._completed.get(trace_id)

    def _put(self, turn):
        self._pending[turn.trace_id] = turn
        if turn.trace_id in self._window:
            self._window.remove(turn.trace_id)
        self._window.append(turn.trace_id)
        self._completed = {key: value for key, value in self._completed.items() if key in self._window}

    def record(self, result: PerceptionResult, *, iteration: int | None = None) -> None:
        with self._lock:
            previous = self._find(result.metrics.trace_id)
            builds = previous.builds if previous else ()
            iteration = iteration or max((b.iteration for b in builds), default=0) + 1
            build = PerceptionBuildSnapshot(
                iteration=iteration, messages=result.messages, records=result.records,
                metrics=result.metrics, applied_admission_message_ids=result.applied_admission_message_ids,
                output_token_limit=result.output_token_limit,
            ).model_copy(deep=True)
            builds = tuple(sorted((*[b for b in builds if b.iteration != iteration], build), key=lambda b: b.iteration))
            exposed = tuple(dict.fromkeys((*(previous.exposed_refs if previous else ()), *result.exposed_refs)))
            self._put(PerceptionTurnTrace(
                trace_id=result.metrics.trace_id, metrics=_aggregate(builds),
                records=build.records, builds=builds, exposed_refs=exposed,
                p3_hit_refs=previous.p3_hit_refs if previous else (),
            ))

    def _replace_build(self, trace_id, iteration, **changes):
        turn = self._find(trace_id)
        if turn is None:
            return
        builds = tuple(b.model_copy(update=changes) if b.iteration == iteration else b for b in turn.builds)
        self._put(turn.model_copy(update={"builds": builds, "metrics": _aggregate(builds)}))

    def record_request(self, trace_id: str, iteration: int, tools: list[dict], *, messages=None) -> None:
        with self._lock:
            changes = {"tools": copy.deepcopy(tools)}
            if messages is not None:
                changes["messages"] = copy.deepcopy(messages)
            self._replace_build(trace_id, iteration, **changes)

    def record_model_call(self, trace_id: str, iteration: int, call: ModelCallSnapshot) -> None:
        with self._lock:
            turn = self._find(trace_id)
            build = next((b for b in turn.builds if b.iteration == iteration), None) if turn else None
            if build is not None:
                self._replace_build(trace_id, iteration, model_calls=(*build.model_calls, call.model_copy(deep=True)))

    def record_auxiliary(self, trace_id, iteration, calls):
        with self._lock:
            turn = self._find(trace_id) or PerceptionTurnTrace(trace_id=trace_id)
            iteration = iteration or max((b.iteration for b in turn.builds), default=1)
            if not any(b.iteration == iteration for b in turn.builds):
                self._put(turn.model_copy(update={"builds": (*turn.builds, PerceptionBuildSnapshot(iteration=iteration))}))
            self._replace_build(trace_id, iteration, auxiliary_calls=tuple(call.model_copy(deep=True) for call in calls))

    def record_failure(self, trace_id: str, iteration: int, error: str) -> None:
        with self._lock:
            turn = self._find(trace_id) or PerceptionTurnTrace(trace_id=trace_id)
            builds = tuple(b for b in turn.builds if b.iteration != iteration)
            previous = next((b for b in turn.builds if b.iteration == iteration), None)
            failed = previous.model_copy(update={"error":error,"messages":None}) if previous else PerceptionBuildSnapshot(iteration=iteration,error=error)
            builds = tuple(sorted((*builds, failed), key=lambda b: b.iteration))
            self._put(turn.model_copy(update={"builds": builds, "metrics": _aggregate(builds)}))

    def finish(self, trace_id: str) -> None:
        """Called only after the database has committed the complete turn."""
        with self._lock:
            turn = self._pending.pop(trace_id, None)
            if turn is not None and trace_id in self._window:
                self._completed[trace_id] = turn

    def record_memory_read(self, trace_id: str, reference: str) -> tuple[str, ...]:
        with self._lock:
            turn = self._find(trace_id)
            if turn is None or reference not in turn.exposed_refs:
                return ()
            hits = tuple(dict.fromkeys((*turn.p3_hit_refs, reference)))
            self._put(turn.model_copy(update={"p3_hit_refs": hits}))
            return (reference,)

    def record_p3_hit(self, trace_id: str, origin_ref: str) -> None:
        self.record_memory_read(trace_id, origin_ref)

    def snapshot(self) -> PerceptionTraceSnapshot:
        with self._lock:
            turns = [self._find(key) for key in self._window]
            turns = [turn for turn in turns if turn is not None]
        metrics = [turn.metrics for turn in turns if turn.metrics is not None]
        exposed = sum(len(turn.exposed_refs) for turn in turns)
        return PerceptionTraceSnapshot(
            n=len(turns),
            budget_usage_p95=_percentile([m.budget_usage for m in metrics], 95),
            budget_usage_p99=_percentile([m.budget_usage for m in metrics], 99),
            dropped_count_p95=_percentile([m.dropped_count for m in metrics], 95),
            dropped_count_p99=_percentile([m.dropped_count for m in metrics], 99),
            deferred_count_p95=_percentile([m.deferred_count for m in metrics], 95),
            deferred_count_p99=_percentile([m.deferred_count for m in metrics], 99),
            p0_dropped_count=sum(m.p0_dropped_count for m in metrics),
            p1_dropped_count=sum(m.p1_dropped_count for m in metrics),
            protected_error_missing_count=sum(m.protected_error_missing_count for m in metrics),
            p3_hit_rate=sum(len(t.p3_hit_refs) for t in turns) / exposed if exposed else 0.0,
        )

    def latest(self) -> PerceptionTurnTrace | None:
        with self._lock:
            turn = self._find(self._window[-1]) if self._window else None
            return turn.model_copy(deep=True) if turn else None

    def get(self, trace_id: str) -> PerceptionTurnTrace | None:
        with self._lock:
            turn = self._find(trace_id)
            return turn.model_copy(deep=True) if turn else None


def _aggregate(builds) -> PerceptionMetrics | None:
    metrics = [build.metrics for build in builds if build.metrics is not None]
    if not metrics:
        return None
    summed = ("admission_judged_count", "admission_judge_failures", "admission_judge_latency_ms",
              "admission_rule_count", "admission_trimmed_count", "admission_deferred_count",
              "admission_saved_tokens", "anchor_builder_attempts")
    peaks = ("budget_usage", "tokens_used", "dropped_count", "deferred_count", "compressed_count",
             "p0_dropped_count", "p1_dropped_count", "protected_error_missing_count", "admission_missed_count")
    return metrics[-1].model_copy(update={
        **{name: sum(getattr(m, name) for m in metrics) for name in summed},
        **{name: max(getattr(m, name) for m in metrics) for name in peaks},
    })


def _percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1])
