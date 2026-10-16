"""SSE 客户端断开时，HTTP worker 应安静清理 EventBus 订阅。"""
from __future__ import annotations

import queue
from types import SimpleNamespace

from agent.types.events import LoopEvent
from agent.core.gateway.http_api import _Handler


class _DisconnectingWriter:
    def write(self, _payload: bytes) -> None:
        raise ConnectionAbortedError(10053, "client disconnected")

    def flush(self) -> None:
        pass


class _EventBus:
    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self.unsubscribed = False

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue()
        subscriber.put(LoopEvent(type="outbound_reply"))
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        self._subscribers.remove(subscriber)
        self.unsubscribed = True


def test_sse_connection_aborted_unsubscribes_without_escaping() -> None:
    bus = _EventBus()
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(gateway=SimpleNamespace(events=bus))
    handler.headers = {}
    handler.wfile = _DisconnectingWriter()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._handle_sse()

    assert bus.unsubscribed
    assert bus._subscribers == []
    assert handler.close_connection is True
