"""Gateway 是渠道停止与出站投递记账的唯一责任方。"""
from __future__ import annotations

import threading

from agent.channel.baseChannel import BaseChannel
from agent.types.channel import InboundEvent, OutboundReply, SessionSource
from agent.gateway.gateway import Gateway


class RecordingChannel(BaseChannel):
    name = "recording"

    def __init__(self, *, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.delivered = threading.Event()
        self.stopped = False

    def start(self):
        self.status["start"] = True

    def stop(self):
        self.stopped = True
        self.status["start"] = False

    def parse_to_InboundEvent(self, raw):
        return InboundEvent(text=str(raw))

    def deliver_to_OutboundEvent(self, reply):
        self.delivered.set()
        if self.fail:
            raise RuntimeError("send failed")


def _reply():
    return OutboundReply(text="ok", source=SessionSource(channel="recording", chat_id="u"))


def test_success_counted_once_and_channel_stopped():
    gateway = Gateway(loop=object())
    channel = RecordingChannel()
    gateway.register_channel(channel)
    gateway.start()
    channel.start()
    try:
        gateway.bus.publish_outbound(_reply())
        assert channel.delivered.wait(2)
    finally:
        gateway.stop()
    assert channel.status["outbound_event_count"] == 1
    assert channel.status["error_count"] == 0
    assert channel.stopped


def test_failed_delivery_counted_once():
    gateway = Gateway(loop=object())
    channel = RecordingChannel(fail=True)
    gateway.register_channel(channel)
    gateway.start()
    channel.start()
    try:
        gateway.bus.publish_outbound(_reply())
        assert channel.delivered.wait(2)
    finally:
        gateway.stop()
    assert channel.status["outbound_event_count"] == 0
    assert channel.status["error_count"] == 1
