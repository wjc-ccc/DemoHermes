"""Gateway 只在事实提交成功后唤醒 Memory reviewer。"""
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.channel.channel_cli import CliChannel
from tests.loop_fakes import make_loop
from agent.core.event_bus import EventBus
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.types.channel import InboundEvent, SessionSource
from agent.gateway.gateway import Gateway
from agent.types.model import ModelResponse
from agent.storage.sqlite.session_store import SqliteSessionStore


class _MockModel:
    name = "mock"

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text="ok")


def _event(text: str) -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(
            channel="cli", chat_id="local", chat_type="dm", user_id="u1"
        ),
    )


def test_gateway_notifies_reviewer_after_commit(tmp_path):
    observed: list[str] = []
    gateway = Gateway(
        loop=make_loop(model=_MockModel(), event_bus=EventBus()),
        store=RuntimeStorage.open(tmp_path / "memory.db").sessions,
        memory_context=MemoryContextLoader(tmp_path / "memory"),
        workers=1,
        on_turn_committed=lambda turn: observed.append(turn.turn_id),
    )
    gateway.register_channel(CliChannel())
    gateway.start()
    try:
        reply = gateway.ask(_event("你好"), timeout=10)
    finally:
        gateway.stop()

    assert reply.text == "ok"
    assert observed == [reply.metadata["turn_id"]]
