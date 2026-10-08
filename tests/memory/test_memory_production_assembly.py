from __future__ import annotations

from agent import main
from agent.model.baseStructure import ModelResponse
from agent.model.registry import ModelRegistry
from agent.core.types import Message, Session


class _Model:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return ModelResponse(text="收到")


def test_production_gateway_registers_memory_tools_and_budget(tmp_path, monkeypatch):
    registry = ModelRegistry()
    model = _Model()
    registry.register(model, as_default=True)
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    monkeypatch.setattr(main, "_memory_paths", lambda: (
        tmp_path / "memory", tmp_path / "agent.db"
    ))
    gateway = main.build_gateway(use_memory=True)
    try:
        assert {"memory_search", "memory_read", "memory_edit"} <= set(
            gateway.loop.tools.list_tools()
        )
        assert gateway._context_budget.model_context_tokens == 32768
        assert gateway._context_budget.reserved_output_tokens == 4096
        assert gateway._memory_context is not None
        oversized = gateway.loop.run_turn(
            Session(session_id="large"), Message(role="user", content_text="长" * 30_000),
            context_budget=gateway._context_budget,
        )
        assert oversized.error == "context_budget_exceeded"
        assert model.calls == 0
    finally:
        gateway.stop()
