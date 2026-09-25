"""冻结的 MemorySnapshot 在一个 turn 内保持不变。"""
from __future__ import annotations

from agent.core.agent_loop import AgentLoop
from agent.core.memory import (
    MarkdownBook,
    MemoryContextLoader,
    MemoryLayer,
    MemoryScope,
    ensure_scope_dir,
)
from agent.core.types import Message, Session
from agent.model.baseStructure import BaseModel, ModelResponse, ToolCallRequest
from agent.tools.base import Tool, ToolCallResult
from agent.tools.registry import ToolRegistry

_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


class _FakeModel(BaseModel):
    name = "fake"

    def __init__(self, script: list[ModelResponse]):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._script.pop(0)


def _book(root) -> MarkdownBook:
    return MarkdownBook(
        ensure_scope_dir(root, _SCOPE) / "MEMORY.md",
        layer=MemoryLayer.USER,
        type="long_term_fact",
        scope=_SCOPE,
        writable=True,
    )


def _memory_system_texts(messages: list[dict]) -> list[str]:
    return [
        message["content"]
        for message in messages
        if message["role"] == "system"
        and "<memory-context>" in str(message.get("content") or "")
    ]


class _ReplaceBookTool(Tool):
    name = "replace_memory_book"
    description = "替换测试用用户手册"

    def __init__(self, book: MarkdownBook):
        self._book = book

    def execute(self, arguments: dict) -> ToolCallResult:
        self._book.replace("# 偏好\n\n中途改为详细回答。\n")
        return ToolCallResult(ok=True, result_text="已更新")


def test_loop_without_memory_snapshot_keeps_existing_behavior():
    model = _FakeModel([ModelResponse(text="你好呀")])
    result = AgentLoop(model=model, event_bus=None).run_turn(
        Session(session_id="s1"), Message(role="user", content_text="你好")
    )

    assert result.completed and result.final_text == "你好呀"
    assert _memory_system_texts(model.calls[0]) == []


def test_all_model_iterations_share_one_frozen_snapshot(tmp_path):
    root = tmp_path / "memory"
    book = _book(root)
    book.replace("# 偏好\n\n先给结论。\n")
    snapshot = MemoryContextLoader(root).freeze(_SCOPE, session_id="s1")

    tools = ToolRegistry()
    tools.register(_ReplaceBookTool(book))
    model = _FakeModel(
        [
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_1", name="replace_memory_book", arguments={}
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(text="已记录", finish_reason="stop"),
        ]
    )

    result = AgentLoop(model=model, tool_registry=tools).run_turn(
        Session(session_id="s1"),
        Message(role="user", content_text="记一下"),
        memory_snapshot=snapshot,
    )

    assert result.completed
    first = _memory_system_texts(model.calls[0])
    second = _memory_system_texts(model.calls[1])
    assert first == second == [snapshot.prompt]
    assert "先给结论" in first[0]
    assert "中途改为详细回答" not in first[0]


def test_next_turn_freeze_observes_book_update(tmp_path):
    root = tmp_path / "memory"
    book = _book(root)
    book.replace("先给结论。")
    loader = MemoryContextLoader(root)
    first = loader.freeze(_SCOPE, session_id="s1")

    book.replace("以后给详细答案。")
    second = loader.freeze(_SCOPE, session_id="s1")

    assert "详细答案" not in first.prompt
    assert "详细答案" in second.prompt
    assert first.revisions != second.revisions
