"""HTTP 访问控制：跨站 Origin 在服务端拒绝、可选 Bearer token、非本机监听必须带 token。"""
from __future__ import annotations

import pytest

from agent.channel.channel_frontier import FrontierChannel
from tests.loop_fakes import make_loop
from agent.core.event_bus import EventBus
from agent.gateway.gateway import Gateway
from agent.gateway.http_api import FrontierHttpServer
from agent.types.model import ModelResponse
from tests.gateway.test_debug_memory_api import _request, _start_server, _stop_server

_FRONTEND = "http://localhost:5173"


class _CountingModel:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return ModelResponse(text="pong")


@pytest.fixture
def running(session_store):
    model = _CountingModel()
    gateway = Gateway(loop=make_loop(model=model, event_bus=EventBus()), store=session_store, workers=1)
    gateway.register_channel(FrontierChannel())
    gateway.start()
    servers = []

    def serve(**options):
        server, port = _start_server(
            gateway, enable_memory_debug=False, allowed_origins=(_FRONTEND,), **options
        )
        servers.append(server)
        return port

    try:
        yield model, serve
    finally:
        for server in servers:
            _stop_server(server)
        gateway.stop()


def test_cross_site_post_is_rejected_before_agent_runs(running):
    model, serve = running
    port = serve()
    status, body, headers = _request(
        port, "POST", "/api/chat",
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        body={"text": "执行一下"},
    )
    assert status == 403
    assert body["error"] == "origin not allowed"
    assert "Access-Control-Allow-Origin" not in headers
    assert model.calls == 0


def test_allowed_origin_is_echoed_instead_of_wildcard(running):
    model, serve = running
    port = serve()
    status, body, headers = _request(
        port, "POST", "/api/chat",
        headers={"Origin": _FRONTEND, "Content-Type": "application/json"},
        body={"text": "ping"},
    )
    assert status == 200 and body["reply"] == "pong"
    assert headers["Access-Control-Allow-Origin"] == _FRONTEND
    assert model.calls == 1


def test_preflight_checks_origin_only(running):
    _, serve = running
    port = serve(api_token="secret")
    allowed, _, headers = _request(port, "OPTIONS", "/api/chat", headers={"Origin": _FRONTEND})
    assert allowed == 204
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    denied, _, _ = _request(port, "OPTIONS", "/api/chat", headers={"Origin": "https://evil.example"})
    assert denied == 403


def test_token_is_required_when_configured(running):
    model, serve = running
    port = serve(api_token="secret")
    missing, _, _ = _request(port, "GET", "/api/status")
    wrong, _, _ = _request(port, "GET", "/api/status", headers={"Authorization": "Bearer nope"})
    ok, body, _ = _request(port, "GET", "/api/status", headers={"Authorization": "Bearer secret"})
    assert (missing, wrong, ok) == (401, 401, 200)
    assert body["model"] == "counting"
    assert model.calls == 0


def test_non_loopback_bind_requires_token(session_store):
    gateway = Gateway(loop=make_loop(model=_CountingModel(), event_bus=EventBus()), store=session_store)
    with pytest.raises(ValueError, match="api_token"):
        FrontierHttpServer(gateway, host="0.0.0.0", port=0)
