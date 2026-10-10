"""outbound_reply 应挂上本轮 turn_id，与 Loop 事件同一轮分组。"""
from __future__ import annotations

import time

from agent.channel.channel_cli import CliChannel
from agent.core.event_bus import EventBus
from tests.loop_fakes import make_loop
from agent.types.channel import InboundEvent, SessionSource
from agent.gateway.gateway import Gateway
from agent.types.model import ModelResponse


class _MockModel:
    name = "mock"

    def __init__(self, script):
        self._script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        return self._script.pop(0)


def test_outbound_reply_event_shares_turn_id_with_loop():
    bus = EventBus()
    q = bus.subscribe(maxsize=200)
    loop = make_loop(model=_MockModel([ModelResponse(text="pong")]), event_bus=bus)
    gw = Gateway(loop=loop, event_bus=bus, workers=1)
    gw.register_channel(CliChannel())
    gw.start()
    try:
        reply = gw.ask(
            InboundEvent(
                text="ping",
                source=SessionSource(
                    channel="cli", chat_id="local", chat_type="dm", user_id="u1"
                ),
            ),
            timeout=10,
        )
        assert reply.text == "pong"

        # 等出站分发线程 publish outbound_reply
        deadline = time.time() + 3
        events = []
        while time.time() < deadline:
            try:
                events.append(q.get(timeout=0.2))
            except Exception:
                if any(e.type == "outbound_reply" for e in events):
                    break

        by_type = {e.type: e for e in events}
        assert "turn_end" in by_type
        assert "outbound_reply" in by_type
        assert by_type["outbound_reply"].turn_id == by_type["turn_end"].turn_id
        assert by_type["outbound_reply"].turn_id
    finally:
        gw.stop()
        bus.unsubscribe(q)
