"""Phase 4.1：装配策略可配置 + 每 turn 记录装配选择。

公共 seam：
  - AssemblyPolicy：include_today_daily / enable_provider_recall / skill_summary_min_chars
  - AssemblyRecorder.record(choice) / latest()
  - Manager.build_context(..., enable_provider_recall=) 按策略装配
"""
from __future__ import annotations

from Agent.evolution.assembly import (
    AssemblyChoice,
    AssemblyPolicy,
    AssemblyRecorder,
)
from Agent.memory import (
    BuiltinMemoryRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.memory.base import MemoryProvider
from Agent.memory.types import ProviderContribution, ProviderRecallRequest

_SCOPE = MemoryScope(kind="personal", key="cli:user:asm1")


class _Provider(MemoryProvider):
    name = "fake"

    def recall(self, request: ProviderRecallRequest) -> ProviderContribution:
        return ProviderContribution(name=self.name, text="recalled-bits")


def test_policy_controls_daily_and_provider(tmp_path):
    """AssemblyPolicy 决定是否注入今日 Daily / 是否做 Provider recall。"""
    manager = MemoryManager(
        root=tmp_path / "memory",
        provider=_Provider(),
    )
    r = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="今日笔记", provenance=MemoryProvenance(source="user_request"),
        )
    )
    assert r.ok

    off = AssemblyPolicy(include_today_daily=False, enable_provider_recall=False)
    snap_off = manager.build_context(
        scope=_SCOPE, query="q", session_id="s1",
        include_today_daily=off.include_today_daily,
        enable_provider_recall=off.enable_provider_recall,
    )
    assert snap_off.daily_prompt is None
    assert snap_off.provider_prompt is None
    assert "今日笔记" not in snap_off.prompt
    assert "recalled-bits" not in snap_off.prompt

    on = AssemblyPolicy(include_today_daily=True, enable_provider_recall=True)
    snap_on = manager.build_context(
        scope=_SCOPE, query="q", session_id="s1",
        include_today_daily=on.include_today_daily,
        enable_provider_recall=on.enable_provider_recall,
    )
    assert snap_on.daily_prompt is not None
    assert snap_on.provider_prompt is not None
    assert "今日笔记" in snap_on.prompt
    assert "recalled-bits" in snap_on.prompt


def test_recorder_keeps_latest_assembly_choice():
    """每 turn 记录装配选择，latest 可读回。"""
    recorder = AssemblyRecorder()
    choice = AssemblyChoice(
        turn_id="t1",
        include_today_daily=True,
        provider_recalled=False,
        skill_summary_chars=120,
        skill_summary_min_chars=80,
    )
    recorder.record(choice)
    assert recorder.latest() == choice
    recorder.record(choice.model_copy(update={"turn_id": "t2", "provider_recalled": True}))
    assert recorder.latest().turn_id == "t2"
    assert recorder.latest().provider_recalled is True
