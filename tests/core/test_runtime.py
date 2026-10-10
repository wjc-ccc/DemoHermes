"""通过真实 Runtime 验证能力开关、生命周期、观测和演化删除。"""
from pathlib import Path
from dataclasses import replace
import subprocess
import sys

import pytest

from agent.runtime import build_runtime
from agent.types.channel import InboundEvent, SessionSource
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.runtime import RuntimeConfig


class CalculatorModel:
    name = "runtime-test"

    def chat(self, messages, tools=None):
        if "长期记忆提炼器" in messages[0].get("content", ""):
            return ModelResponse(text="[]")
        if any(message.get("role") == "tool" for message in messages):
            return ModelResponse(text="16")
        return ModelResponse(tool_calls=[ToolCallRequest(
            id="calculation", name="calculator", arguments={"expression": "(3+5)*2"},
        )])


class Models:
    def __init__(self):
        self.model = CalculatorModel()

    def get(self, *args):
        return self.model


@pytest.mark.parametrize("memory,action", [(False, False), (True, False), (False, True), (True, True)])
def test_runtime_capability_matrix_and_committed_tool_facts(tmp_path, monkeypatch, memory, action):
    monkeypatch.setenv("JEVMODEL_API_KEY", "")
    runtime = build_runtime(RuntimeConfig(use_memory=memory, use_action=action, data_root=tmp_path), model_registry=Models())
    before = {p: p.read_bytes() for p in (tmp_path / "skills").rglob("SKILL.md")}
    events = runtime.events.subscribe()
    runtime.start()
    runtime.start()
    try:
        reply = runtime.gateway.ask(InboundEvent(text="计算", source=SessionSource(user_id="u")), timeout=10)
        assert reply.text == "16" and "error" not in reply.metadata
        description = runtime.describe()
        assert description.memory is memory and description.action is action
        assert description.storage == "sqlite"
        assert description.database == str(tmp_path / "agent.db")
        assert (runtime.loop.precision.config.budget is not None) is memory
        assert {"calculator", "use_skill", "skill_manage"} <= set(description.tools)
        assert ({"memory_read", "memory_search", "memory_edit"} <= set(description.tools)) is memory
        turn = runtime.precision_trace.get(reply.metadata["turn_id"])
        assert len(turn.builds) == 2
        assert any(event.type == "tool_call_result" for event in list(events.queue))
        record = runtime.storage.executions.get_turn(reply.metadata["turn_id"])
        assert any(message["role"] == "user" and message["content_text"] == "计算" for message in record["messages"])
        assert any(message["role"] == "assistant" and message["content_text"] == "16" for message in record["messages"])
        call = record["tool_calls"][0]
        assert call["tool_name"] == "calculator"
        if action:
            assert call["metadata"]["action"]["effect_state"] == "succeeded"
            assert call["metadata"]["provider_tool_call_id"] == "calculation"
        with runtime.storage.db.read() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert ("action_items" in tables) is action
            assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type LIKE 'skill.%'").fetchone()[0] == 0
    finally:
        runtime.stop()
        runtime.stop()
    assert before == {p: p.read_bytes() for p in (tmp_path / "skills").rglob("SKILL.md")}


def test_memory_disabled_conversation_survives_runtime_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVMODEL_API_KEY", "")
    config = RuntimeConfig(use_memory=False, use_action=False, data_root=tmp_path)
    source = SessionSource(user_id="u")
    runtime = build_runtime(config, model_registry=Models())
    runtime.start()
    try:
        first_reply = runtime.gateway.ask(InboundEvent(text="计算", source=source), timeout=10)
        assert first_reply.text == "16" and "error" not in first_reply.metadata
        saved = runtime.storage.sessions.get_session(first_reply.metadata["session_id"])
    finally:
        runtime.stop()

    class HistoryModel:
        name = "history-test"

        def chat(self, messages, tools=None):
            self.messages = messages
            return ModelResponse(text="继续")

    models = Models()
    models.model = HistoryModel()
    restarted = build_runtime(config, model_registry=models)
    restarted.start()
    try:
        restored = restarted.storage.sessions.find(saved.session_key)
        assert restored.model_dump(mode="json") == saved.model_dump(mode="json")
        second_reply = restarted.gateway.ask(InboundEvent(text="接着聊", source=source), timeout=10)
        assert second_reply.text == "继续" and "error" not in second_reply.metadata
        assert second_reply.metadata["session_id"] == first_reply.metadata["session_id"]
        assert any(message["role"] == "user" and message.get("content") == "计算" for message in models.model.messages)
        assert any(message["role"] == "assistant" and message.get("content") == "16" for message in models.model.messages)
        assert len(restarted.storage.executions.list_turns(restored.session_id)) == 2
        assert restarted.memory is None
        assert restarted.loop.precision.config.budget is None
    finally:
        restarted.stop()


def test_memory_disabled_failed_turn_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVMODEL_API_KEY", "")

    class FailingModel:
        name = "failure-test"

        def chat(self, messages, tools=None):
            raise RuntimeError("model unavailable")

    models = Models()
    models.model = FailingModel()
    runtime = build_runtime(RuntimeConfig(use_memory=False, use_action=False, data_root=tmp_path), model_registry=models)
    runtime.loop.config = replace(runtime.loop.config, model_retries=0)
    runtime.start()
    try:
        reply = runtime.gateway.ask(InboundEvent(text="失败请求", source=SessionSource(user_id="u")), timeout=10)
        assert "error" in reply.metadata
        record = runtime.storage.executions.get_record(reply.metadata["turn_id"])
        assert record is not None and not record.completed
        assert record.error == reply.metadata["error"]
        assert "model unavailable" in record.error
        session = runtime.storage.sessions.get_session(reply.metadata["session_id"])
        assert any(message.role == "user" and message.content_text == "失败请求" for message in session.messages)
    finally:
        runtime.stop()


def test_runtimes_own_independent_event_and_context_windows(tmp_path):
    runtimes = [build_runtime(RuntimeConfig(use_memory=False, data_root=tmp_path / str(i)), model_registry=Models()) for i in range(2)]
    try:
        assert runtimes[0].events is not runtimes[1].events
        assert runtimes[0].precision_trace is not runtimes[1].precision_trace
        runtimes[0].start()
        reply = runtimes[0].gateway.ask(InboundEvent(text="计算", source=SessionSource(user_id="u")), timeout=10)
        assert runtimes[0].precision_trace.get(reply.metadata["turn_id"]) is not None
        assert runtimes[1].precision_trace.snapshot().n == 0
    finally:
        for runtime in runtimes:
            runtime.stop()


def test_disabled_action_does_not_load_action_implementation(tmp_path):
    script = """
import sys
from pathlib import Path
from agent.runtime import build_runtime
from agent.types.runtime import RuntimeConfig
from agent.types.model import ModelResponse
class Models:
    def get(self): return type('Model', (), {'name': 'fake', 'chat': lambda self,*a,**k: ModelResponse(text='ok')})()
runtime=build_runtime(RuntimeConfig(use_memory=False,use_action=False,data_root=Path(sys.argv[1])),model_registry=Models())
assert not any(name.startswith('agent.action') for name in sys.modules)
assert (Path(sys.argv[1])/'agent.db').exists()
with runtime.storage.db.read() as conn:
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'action_%'").fetchone()[0] == 0
runtime.stop()
"""
    result = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
