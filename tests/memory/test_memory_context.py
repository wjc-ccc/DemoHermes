"""Slice 7: 不可变 turn 快照接入 Loop——同 turn 所有模型迭代看到相同记忆（§1.1-6 / ADR-3）。"""
import threading

from Agent.core.loop import AgentLoop
from Agent.core.types import Message, Session
from Agent.memory import (
    BuiltinMemoryRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.model.baseStructure import BaseModel, ModelResponse, ToolCallRequest
from Agent.tools.base import Tool, ToolCallResult
from Agent.tools.registry import ToolRegistry

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="user_request")


class _FakeModel(BaseModel):
    """按脚本返回的假模型：每次 chat 弹出下一个 ModelResponse 并记录请求。"""

    name = "fake"

    def __init__(self, script: list[ModelResponse]):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._script.pop(0)


def _add_profile(manager: MemoryManager, content: str) -> None:
    result = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content=content, provenance=_PROVENANCE,
        )
    )
    assert result.ok, result.message


def _user_message(text: str) -> Message:
    return Message(role="user", content_text=text)


def _memory_system_texts(messages: list[dict]) -> list[str]:
    return [
        m["content"] for m in messages
        if m["role"] == "system" and "<memory-context>" in m["content"]
    ]


def test_loop_without_memory_snapshot_keeps_existing_behavior(tmp_path):
    """不传 memory_snapshot 的 run_turn 行为与现状一致：无 memory 围栏，正常回复。"""
    model = _FakeModel([ModelResponse(text="你好呀")])
    loop = AgentLoop(model=model, event_bus=None)
    session = Session(session_id="s1")

    result = loop.run_turn(session, _user_message("你好"))

    assert result.completed and result.final_text == "你好呀"
    assert len(model.calls) == 1
    assert _memory_system_texts(model.calls[0]) == []


def test_snapshot_does_not_change_after_mid_turn_memory_edit(tmp_path):
    """§1.1-6：快照是创建时的不可变返回值；turn 中发生的更新从下一 turn 才可见。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "初始偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    prompt_before = snapshot.prompt

    # turn 中途发生写入（模拟工具/reviewer 改了 memory）
    _add_profile(manager, "中途新增：周三改口要详细版")

    # 已创建快照逐字不变，中途更新不可见
    assert snapshot.prompt == prompt_before
    assert "中途新增：周三改口要详细版" not in snapshot.prompt
    assert "初始偏好：先给结论" in snapshot.prompt


class _UpdateMemoryTool(Tool):
    """经公开 Tool seam 调真实 MemoryManager 改 memory 的最小测试工具。"""

    name = "update_memory"
    description = "记录一条用户偏好"

    def __init__(self, manager: MemoryManager):
        self._manager = manager

    def execute(self, arguments: dict) -> ToolCallResult:
        _add_profile(self._manager, "中途新增：周三改口要详细版")
        return ToolCallResult(ok=True, result_text="已记录")


def test_all_model_iterations_in_one_turn_receive_same_snapshot(tmp_path):
    """§1.1-6 / §7 最小 Green：Loop 内每次创建 Builder 都传同一快照对象。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "初始偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")

    tools = ToolRegistry()
    tools.register(_UpdateMemoryTool(manager))
    model = _FakeModel([
        ModelResponse(
            text="",
            tool_calls=[ToolCallRequest(id="call_1", name="update_memory", arguments={})],
            finish_reason="tool_calls",
        ),
        ModelResponse(text="已记录偏好", finish_reason="stop"),
    ])
    loop = AgentLoop(model=model, tool_registry=tools)
    session = Session(session_id="s1")

    result = loop.run_turn(session, _user_message("记一下"), memory_snapshot=snapshot)

    assert result.completed
    assert len(model.calls) == 2  # 第一次要调工具，第二次纯文本
    first_memory = _memory_system_texts(model.calls[0])
    second_memory = _memory_system_texts(model.calls[1])
    # 两次模型迭代看到的是同一段 memory block
    assert len(first_memory) == 1 and len(second_memory) == 1
    assert first_memory[0] == second_memory[0] == snapshot.prompt
    # 工具中途写入的内容本 turn 两次迭代都不可见，初始偏好都在
    for text in (first_memory[0], second_memory[0]):
        assert "初始偏好：先给结论" in text
        assert "中途新增：周三改口要详细版" not in text


def test_next_turn_receives_new_snapshot(tmp_path):
    """调用方每 turn build 一次：turn 2 的快照能看到 turn 1 之后的写入。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "初始偏好：先给结论")
    session = Session(session_id="s1")

    model1 = _FakeModel([ModelResponse(text="好")])
    AgentLoop(model=model1).run_turn(
        session, _user_message("第一句"),
        memory_snapshot=manager.build_context(scope=_SCOPE, query="一", session_id="s1"),
    )
    _add_profile(manager, "第二句前补的偏好")

    model2 = _FakeModel([ModelResponse(text="行")])
    AgentLoop(model=model2).run_turn(
        session, _user_message("第二句"),
        memory_snapshot=manager.build_context(scope=_SCOPE, query="二", session_id="s1"),
    )

    turn1_memory = _memory_system_texts(model1.calls[0])[0]
    turn2_memory = _memory_system_texts(model2.calls[0])[0]
    assert "第二句前补的偏好" not in turn1_memory
    assert "第二句前补的偏好" in turn2_memory


def test_two_concurrent_sessions_in_same_scope_do_not_share_turn_snapshot_state(tmp_path):
    """ADR-3：Manager 不缓存 turn 快照；同 scope 两个并发 session 各持各的不可变快照。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "共同偏好：先给结论")
    snap1 = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    _add_profile(manager, "只有 session2 该看到的偏好")
    snap2 = manager.build_context(scope=_SCOPE, query="q", session_id="s2")

    barrier = threading.Barrier(2)
    models = [_FakeModel([ModelResponse(text="好")]) for _ in range(2)]
    snaps = [snap1, snap2]
    errors: list[Exception] = []

    def run(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            AgentLoop(model=models[idx]).run_turn(
                Session(session_id=f"s{idx + 1}"), _user_message("你好"),
                memory_snapshot=snaps[idx],
            )
        except Exception as exc:  # pragma: no cover - 收进主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors

    mem1 = _memory_system_texts(models[0].calls[0])[0]
    mem2 = _memory_system_texts(models[1].calls[0])[0]
    # session1 的快照停留在编辑前；session2 看到编辑后；互不串扰
    assert "只有 session2 该看到的偏好" not in mem1
    assert "只有 session2 该看到的偏好" in mem2
    assert "共同偏好：先给结论" in mem1 and "共同偏好：先给结论" in mem2
