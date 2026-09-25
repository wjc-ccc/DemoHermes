"""EvolutionSignalCollector — 从 EventBus 收集轻量 turn 信号（8.04 Phase 0.4）。

供 Reflector 使用，避免二次扫全库。观测旁路：丢事件可接受。
"""
from __future__ import annotations

import threading

from ..core.event_bus import EventBus, LoopEvent
from .types import EvolutionSignal


class EvolutionSignalCollector:
    """订阅 EventBus，按 turn_id 聚合 completed / tool 失败 / 迭代次数。"""

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._q = event_bus.subscribe(maxsize=2000)
        self._lock = threading.Lock()
        self._by_turn: dict[str, dict] = {}
        self._thread = threading.Thread(
            target=self._loop, name="evolution-signals", daemon=True,
        )
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe(self._q)

    def get(self, turn_id: str) -> EvolutionSignal | None:
        with self._lock:
            raw = self._by_turn.get(turn_id)
            if raw is None:
                return None
            return EvolutionSignal(
                turn_id=turn_id,
                completed=bool(raw.get("completed", True)),
                interrupted=bool(raw.get("interrupted", False)),
                error=raw.get("error"),
                iteration_count=int(raw.get("iteration_count", 0)),
                tool_failures=tuple(raw.get("tool_failures", ())),
                tool_sequence=tuple(raw.get("tool_sequence", ())),
                skills_used=tuple(raw.get("skills_used", ())),
                recovered_from_failure=bool(raw.get("recovered_from_failure", False)),
            )

    def _loop(self) -> None:
        while self._running:
            try:
                event = self._q.get(timeout=0.5)
            except Exception:
                continue
            if not isinstance(event, LoopEvent):
                continue
            self._ingest(event)

    def _ingest(self, event: LoopEvent) -> None:
        tid = event.turn_id or ""
        if not tid:
            return
        with self._lock:
            slot = self._by_turn.setdefault(
                tid,
                {"tool_failures": [], "tool_sequence": [], "skills_used": []},
            )
            data = event.data or {}
            if event.type == "llm_request":
                slot["iteration_count"] = max(
                    int(slot.get("iteration_count", 0)),
                    int(data.get("iteration", 0) or data.get("iteration_count", 0)),
                )
            elif event.type in ("tool_call_start", "tool_call_result"):
                name = str(
                    data.get("name")
                    or data.get("tool_name")
                    or ""
                ).strip()
                if event.type == "tool_call_start" and name:
                    seq: list = slot["tool_sequence"]
                    seq.append(name)
                    if name == "use_skill":
                        skill = str(
                            data.get("skill_name")
                            or (data.get("arguments") or {}).get("skill_name")
                            or ""
                        ).strip()
                        if skill:
                            used: list = slot["skills_used"]
                            used.append(skill)
                if event.type == "tool_call_result":
                    ok = data.get("ok", data.get("success", True))
                    if ok is False:
                        err_name = name or "tool"
                        err = str(
                            data.get("error")
                            or data.get("result_preview")
                            or "failed"
                        )
                        failures: list = slot["tool_failures"]
                        failures.append(f"{err_name}: {err}")
                        slot["had_failure"] = True
                    elif slot.get("had_failure"):
                        # Hermes：errors overcome — 失败后再次成功
                        slot["recovered_from_failure"] = True
                    # use_skill 成功结果里也可能带 skill_name
                    if name == "use_skill" and ok is not False:
                        skill = str(
                            data.get("skill_name")
                            or (data.get("data") or {}).get("skill_name")
                            or ""
                        ).strip()
                        if skill and skill not in slot["skills_used"]:
                            slot["skills_used"].append(skill)
            elif event.type in ("turn_end", "turn_error"):
                slot["completed"] = event.type == "turn_end"
                slot["interrupted"] = bool(data.get("interrupted", False))
                if event.type == "turn_end" and slot.get("had_failure"):
                    slot["recovered_from_failure"] = True
                if event.type == "turn_error":
                    slot["error"] = str(data.get("error") or "turn_error")
                    slot["completed"] = False
