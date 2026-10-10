from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from tests.loop_fakes import make_loop
from agent.types.memory.scope import MemoryScope
from agent.types.conversation import Message, Session
from agent.types.model import ModelResponse, ToolCallRequest
from agent.tools.base import Tool
from agent.types.tools import ToolCallResult
from agent.tools.registry import ToolRegistry


class _Search(Tool):
    name = "memory_search"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

    def __init__(self):
        self.calls = []
        self.lock = Lock()

    def execute(self, arguments, *, context):
        with self.lock:
            self.calls.append((arguments["query"], context.memory_scope.storage_key))
        return ToolCallResult(ok=True, result_text="[]")


class _Model:
    def chat(self, messages, tools=None):
        if any(message.get("role") == "tool" for message in messages):
            return ModelResponse(text="完成")
        query = next(message["content"] for message in reversed(messages)
                     if message.get("role") == "user")
        return ModelResponse(tool_calls=[ToolCallRequest(
            id="call", name="memory_search",
            arguments={"query": query, "_scope": "forged"},
        )])


def test_parallel_users_pass_distinct_runtime_scope_to_memory_tools():
    search = _Search()
    registry = ToolRegistry()
    registry.register(search)
    loop = make_loop(model=_Model(), tools=registry)
    scopes = (MemoryScope(tenant_id="cli", user_id="alice"),
              MemoryScope(tenant_id="cli", user_id="bob"))

    def run(index):
        return loop.run_turn(
            Session(session_id=f"session-{index}"),
            Message(role="user", content_text=f"query-{index}"),
            memory_scope=scopes[index],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert all(result.completed for result in results)
    assert set(search.calls) == {
        ("query-0", scopes[0].storage_key),
        ("query-1", scopes[1].storage_key),
    }
