"""Phase 0.2：Gateway commit 成功后调用 MemoryManager.sync_turn（8.04）。

公共 seam：Gateway + MemoryManager.sync_turn。
Fake 仅用于 Model 与 Provider；不 mock Manager/Gateway。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Agent.channel.channel_cli import CliChannel
from Agent.core.events import EventBus
from Agent.core.loop import AgentLoop
from Agent.core.types import InboundEvent, SessionSource
from Agent.gateway import Gateway
from Agent.memory import MemoryManager
from Agent.memory.base import MemoryProvider
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.types import ProviderContribution, ProviderRecallRequest, TurnRecord
from Agent.model.baseStructure import ModelResponse


class _MockModel:
    name = "mock"

    def __init__(self, script):
        self._script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        item = self._script.pop(0)
        return item


class _RecordingProvider(MemoryProvider):
    name = "recording"

    def __init__(self) -> None:
        self.synced: list[str] = []

    def recall(self, request: ProviderRecallRequest) -> ProviderContribution:
        return ProviderContribution(name=self.name, text="")

    def sync_turn(self, turn: TurnRecord, session) -> None:
        self.synced.append(turn.turn_id)


def _event(text: str) -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(channel="cli", chat_id="local", chat_type="dm", user_id="u1"),
    )


def test_gateway_commit_calls_sync_turn_once(tmp_path):
    """commit_turn 成功后 Gateway 调用 manager.sync_turn；同 turn 只同步一次。"""
    provider = _RecordingProvider()
    manager = MemoryManager(root=tmp_path / "memory", provider=provider)
    store = SqliteSessionStore(tmp_path / "memory.db")
    loop = AgentLoop(model=_MockModel([ModelResponse(text="ok")]), event_bus=EventBus())
    gw = Gateway(loop=loop, store=store, memory=manager, workers=1)
    gw.register_channel(CliChannel(quiet=True))
    gw.start()
    try:
        reply = gw.ask(_event("你好"), timeout=10)
    finally:
        gw.stop()

    assert reply.text == "ok"
    assert len(provider.synced) == 1
    assert provider.synced[0]  # 非空 turn_id
