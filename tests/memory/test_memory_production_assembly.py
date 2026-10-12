from __future__ import annotations
from agent.types.runtime import RuntimeConfig

from agent import main
from agent.runtime import build_runtime
from agent.types.model import ModelResponse
from agent.model.registry import ModelRegistry
from agent.types.conversation import Message, Session


class _Model:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return ModelResponse(text="收到")


def test_production_gateway_registers_memory_tools_and_budget(tmp_path, monkeypatch):
    registry = ModelRegistry()
    model = _Model()
    registry.register(model, as_default=True)
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))

    runtime = build_runtime(RuntimeConfig(use_memory=True, data_root=tmp_path))
    try:
        assert {"memory_search", "memory_read", "memory_edit"} <= set(
            runtime.tools.list_tools()
        )
        assert runtime.loop.perception.config.budget.model_context_tokens == 32768
        assert runtime.loop.perception.config.budget.reserved_output_tokens == 4096
        assert runtime.gateway._memory_context is not None
        oversized = runtime.gateway.loop.run_turn(
            Session(session_id="large"), Message(role="user", content_text="长" * 30_000),
        )
        assert oversized.error == "context_budget_exceeded"
        assert model.calls == 0
    finally:
        runtime.stop()
