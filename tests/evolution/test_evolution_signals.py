"""Phase 0.4：EventBus → EvolutionSignalCollector。"""
from __future__ import annotations

import time

from agent.core.event_bus import EventBus, LoopEvent
from agent.evolution.signals import EvolutionSignalCollector


def test_signal_collector_records_tool_failure():
    bus = EventBus()
    collector = EvolutionSignalCollector(bus)
    collector.start()
    try:
        bus.publish(LoopEvent(
            type="tool_call_result",
            turn_id="t_sig",
            data={"ok": False, "tool_name": "bash", "error": "timeout"},
        ))
        bus.publish(LoopEvent(
            type="turn_end",
            turn_id="t_sig",
            data={},
        ))
        # 后台线程 ingest
        deadline = time.time() + 2.0
        signal = None
        while time.time() < deadline:
            signal = collector.get("t_sig")
            if signal and signal.tool_failures:
                break
            time.sleep(0.05)
    finally:
        collector.stop()

    assert signal is not None
    assert signal.tool_failures == ("bash: timeout",)
    assert signal.completed is True


def test_signal_collector_records_tool_sequence_and_skills_used():
    bus = EventBus()
    collector = EvolutionSignalCollector(bus)
    collector.start()
    try:
        bus.publish(LoopEvent(
            type="tool_call_start",
            turn_id="t_seq",
            data={"name": "use_skill", "arguments": {"skill_name": "weekly_report"}},
        ))
        bus.publish(LoopEvent(
            type="tool_call_result",
            turn_id="t_seq",
            data={"ok": True, "name": "use_skill"},
        ))
        bus.publish(LoopEvent(
            type="tool_call_start",
            turn_id="t_seq",
            data={"name": "calculator", "arguments": {}},
        ))
        bus.publish(LoopEvent(
            type="tool_call_result",
            turn_id="t_seq",
            data={"ok": False, "name": "calculator", "error": "boom"},
        ))
        deadline = time.time() + 2.0
        signal = None
        while time.time() < deadline:
            signal = collector.get("t_seq")
            if signal and signal.tool_sequence and signal.tool_failures:
                break
            time.sleep(0.05)
    finally:
        collector.stop()

    assert signal is not None
    assert signal.tool_sequence == ("use_skill", "calculator")
    assert signal.skills_used == ("weekly_report",)
    assert signal.tool_failures == ("calculator: boom",)
