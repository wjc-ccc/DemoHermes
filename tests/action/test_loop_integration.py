from agent.types.runtime import RuntimeConfig
import uuid
from concurrent.futures import ThreadPoolExecutor
from agent.core.action.bootstrap import build_action_runtime
from agent.types.action.config import ActionConfig
from agent.types.action.policy import Toolset
from agent.core.action.storage.migrations import schema_directory
from tests.loop_fakes import make_loop
from agent.types.conversation import Session, Message
from agent.types.channel import InboundEvent, SessionSource
from agent.types.memory.scope import MemoryScope
from agent.types.action.policy import AssemblyPolicy
from agent.runtime import build_runtime
from agent.types.model import ModelResponse, ToolCallRequest
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.tools.calculator_tools import CalculatorTool
from agent.tools.registry import ToolRegistry


class CalculatorModel:
    supports_images = False
    def __init__(self):
        self.tools_seen = []
    def chat(self, messages, tools=None):
        self.tools_seen.append(tools)
        if any(message.get("role") == "tool" for message in messages):
            return ModelResponse(text="16")
        if tools:
            return ModelResponse(tool_calls=[ToolCallRequest(id=uuid.uuid4().hex, name="calculator",
                                                           arguments={"expression": "(3+5)*2"})])
        return ModelResponse(text="无工具")


def test_loop_uses_action_and_explicit_empty_toolset(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db", extra_schema_dirs=(schema_directory(),))
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    config = ActionConfig(toolsets=(Toolset(tools=("calculator",)), Toolset(name="empty")))
    runtime = build_action_runtime(storage, registry, config=config)
    model = CalculatorModel()
    loop = make_loop(model=model, tools=registry, action=runtime)
    try:
        session = Session(author_id="u")
        assert loop.run_turn(session, Message(role="user", content_text="计算")).completed
        assert session.tool_calls[0].action_evidence.effect_state == "succeeded"
        assert len(runtime.ledger.attempts()) == 1
        model.tools_seen.clear()
        loop.run_turn(Session(), Message(role="user", content_text="空工具"),
                      assembly_policy=AssemblyPolicy(toolset="empty"))
        assert model.tools_seen == [None]
    finally:
        runtime.stop()


def test_enabled_gateway_without_memory_still_persists_ledger(tmp_path):
    class Models:
        def get(self, *args):
            return CalculatorModel()
    runtime = build_runtime(RuntimeConfig(use_memory=False, use_action=True, data_root=tmp_path), model_registry=Models())
    runtime.start()
    try:
        reply = runtime.gateway.ask(InboundEvent(text="计算", source=SessionSource(user_id="u")), timeout=10)
        assert reply.text == "16" and "error" not in reply.metadata
        assert len(runtime.action.ledger.list()) == 1
    finally:
        runtime.stop()
    assert RuntimeStorage.open(tmp_path / "agent.db").db.path.exists()


def test_two_users_share_loop_without_sharing_action_context(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db", extra_schema_dirs=(schema_directory(),))
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime = build_action_runtime(storage, registry)
    loop = make_loop(model=CalculatorModel(), tools=registry, action=runtime)
    def run(user):
        return loop.run_turn(Session(author_id=user), Message(role="user", content_text="计算"),
                             memory_scope=MemoryScope(tenant_id="cli", user_id=user))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert all(result.completed for result in pool.map(run, ("alice", "bob")))
        assert len({item.scope_key for item in runtime.ledger.list()}) == 2
    finally:
        runtime.stop()
