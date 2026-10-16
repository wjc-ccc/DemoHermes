"""冻结的 MemorySnapshot 在一个 turn 内保持不变。"""
from __future__ import annotations

from tests.loop_fakes import make_loop
from agent.core.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.types.memory.layers import MemoryLayer
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_scope import ensure_scope_dir
from agent.types.conversation import Message, Session
from agent.core.model.baseStructure import BaseModel
from agent.types.model import ModelResponse, ToolCallRequest
from agent.core.tools.base import Tool
from agent.types.tools import ToolCallResult
from agent.core.tools.registry import ToolRegistry

_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


class _FakeModel(BaseModel):
    name = "fake"

    def __init__(self, script: list[ModelResponse]):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._script.pop(0)


def _book(root) -> MarkdownDocuments:
    return MarkdownDocuments(root)


def _replace_book(book, content):
    return book.replace_user(_SCOPE, content, expected_hash=book.read(_SCOPE).disk_hash, actor="human", reason="test edit")


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

    def __init__(self, book: MarkdownDocuments):
        self._book = book

    def execute(self, arguments: dict, *, context) -> ToolCallResult:
        _replace_book(self._book, "# 偏好\n\n中途改为详细回答。\n")
        return ToolCallResult(ok=True, result_text="已更新")


def test_loop_without_memory_snapshot_keeps_existing_behavior():
    model = _FakeModel([ModelResponse(text="你好呀")])
    result = make_loop(model=model, event_bus=None).run_turn(
        Session(session_id="s1"), Message(role="user", content_text="你好")
    )

    assert result.completed and result.final_text == "你好呀"
    assert _memory_system_texts(model.calls[0]) == []


def test_all_model_iterations_share_one_frozen_snapshot(tmp_path):
    root = tmp_path / "memory"
    book = _book(root)
    _replace_book(book, "# 偏好\n\n先给结论。\n")
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

    result = make_loop(model=model, tools=tools).run_turn(
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
    _replace_book(book, "先给结论。")
    loader = MemoryContextLoader(root)
    first = loader.freeze(_SCOPE, session_id="s1")

    _replace_book(book, "以后给详细答案。")
    second = loader.freeze(_SCOPE, session_id="s1")

    assert "详细答案" not in first.prompt
    assert "详细答案" in second.prompt
    assert first.revisions != second.revisions
