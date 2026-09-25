"""Phase 0.1：Gateway 把 context_budget 传入 run_turn（8.04）。

公共 seam：Gateway(context_budget=...) → AgentLoop → 可观测 TurnResult.error。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.storage.sqlite import RuntimeStorage
from agent.channel.channel_cli import CliChannel
from agent.core.context_budget import ContextBudget
from agent.core.event_bus import EventBus
from agent.core.agent_loop import AgentLoop
from agent.core.memory import MemoryContextLoader
from agent.core.types import InboundEvent, SessionSource
from agent.gateway import Gateway
from agent.storage.sqlite import SqliteSessionStore
from agent.model.baseStructure import ModelResponse


class _MockModel:
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return ModelResponse(text="不应到达")


class _HugeEstimator:
    """任意消息都报超大成本，迫使预算裁剪后仍失败。"""

    def estimate_messages(self, messages) -> int:
        return 10_000


def _event(text: str) -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(channel="cli", chat_id="local", chat_type="dm", user_id="u1"),
    )


def test_gateway_passes_context_budget_and_surfaces_exceeded(tmp_path):
    """配置极小 budget 时 Gateway 路径返回 context_budget_exceeded，且不调模型。"""
    memory_context = MemoryContextLoader(root=tmp_path / "memory")
    store = RuntimeStorage.open(tmp_path / "memory.db").sessions
    model = _MockModel()
    loop = AgentLoop(model=model, event_bus=EventBus())
    budget = ContextBudget(model_context_tokens=20, reserved_output_tokens=10)
    gw = Gateway(
        loop=loop,
        store=store,
        memory_context=memory_context,
        workers=1,
        context_budget=budget,
        token_estimator=_HugeEstimator(),
    )
    gw.register_channel(CliChannel(quiet=True))
    gw.start()
    try:
        reply = gw.ask(_event("你好"), timeout=10)
    finally:
        gw.stop()

    assert model.calls == 0
    assert "context_budget_exceeded" in (reply.text or "")
    assert (reply.metadata or {}).get("error") == "context_budget_exceeded"
