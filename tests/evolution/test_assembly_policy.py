"""装配选择的旁路记录。

Memory 的冻结来源已经收敛到 MemoryContextLoader；AssemblyRecorder 只记录
Precision 实际采用的装配选择，不再控制 Daily 或 Provider 记忆读取。
"""
from __future__ import annotations

from agent.evolution.assembly import (
    AssemblyChoice,
    AssemblyRecorder,
)


def test_recorder_keeps_latest_assembly_choice():
    """每 turn 记录装配选择，latest 可读回。"""
    recorder = AssemblyRecorder()
    choice = AssemblyChoice(
        turn_id="t1",
        skill_summary_chars=120,
        skill_summary_min_chars=80,
    )
    recorder.record(choice)
    assert recorder.latest() == choice
    recorder.record(choice.model_copy(update={"turn_id": "t2"}))
    assert recorder.latest().turn_id == "t2"
