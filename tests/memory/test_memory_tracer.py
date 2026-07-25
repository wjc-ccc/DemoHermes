"""Slice 1: tracer bullet——精选偏好经 MemoryManager 落盘后进入下一轮 Context。"""
from Agent.core.context_builder import ContextBuilder
from Agent.core.types import Session
from Agent.memory import (
    BuiltinMemoryMutation,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)


def test_added_profile_memory_reaches_context_builder_system_messages(tmp_path):
    # arrange：真实 tmp root，personal scope
    manager = MemoryManager(root=tmp_path / "memory")
    scope = MemoryScope(kind="personal", key="cli:user:u1")

    # act：apply add → build_context → snapshot 传给真实 ContextBuilder
    result = manager.apply(
        BuiltinMemoryMutation(
            scope=scope,
            target="profile",
            action="add",
            content="用户偏好先给结论",
            provenance=MemoryProvenance(source="user_request"),
        )
    )
    assert result.ok

    snapshot = manager.build_context(scope, query="帮我写方案", session_id="s1")
    messages = ContextBuilder(
        Session(session_id="s1"), memory_context=snapshot
    ).building_context()

    # assert：Builder 产出的一个 system message 含该字面量和 memory 围栏
    system_texts = [m["content"] for m in messages if m["role"] == "system"]
    assert any(
        "用户偏好先给结论" in text and "<memory-context>" in text
        for text in system_texts
    )
