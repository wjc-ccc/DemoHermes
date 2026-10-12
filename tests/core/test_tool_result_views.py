import time
from types import SimpleNamespace
from agent.core.building_messages import build_tool_message, build_assistant_message
from agent.core.tool_dispatch import ToolDispatch
from agent.types.model import ToolCallRequest
from agent.types.tools import ToolCallResult, ToolDispatchItem, ToolInvocationContext


def dispatch(result):
    ticket = ToolDispatchItem(request=ToolCallRequest(id="provider", name="custom_probe", arguments={}),
                              invocation=ToolInvocationContext(), effective_args={}, fact_id="fact",
                              started_at=time.time(), mode="return", result=result)
    return ToolDispatch(SimpleNamespace(tool_result=lambda *args: None)).dispatch(ticket)


def test_structured_only_result_is_readable_without_losing_machine_fields():
    data = {"rows": [{"name": "样本", "count": 3}], "finished": True}
    record = dispatch(ToolCallResult(ok=True, data=data))
    message = build_tool_message(record)
    assert record.result_data == data
    assert "工具：custom_probe" in message.content_text
    assert "name: 样本" in message.content_text and "count: 3" in message.content_text
    assert not message.content_text.lstrip().startswith("{")


def test_explicit_json_file_text_keeps_its_original_bytes():
    raw = '{\n  "filename": "原文.json", "value": 3\n}\n'
    record = dispatch(ToolCallResult(ok=True, result_text=raw, data={"path": "原文.json"}))
    assert record.result_text == raw and build_tool_message(record).content_text == raw


def test_error_only_tool_fact_remains_searchable_and_readable_after_restart(tmp_path):
    from agent.storage.sqlite.runtime_storage import RuntimeStorage
    from agent.storage.sqlite.memory_retrieval import MemoryRetriever
    from agent.types.conversation import Session, Message, ToolCall
    from agent.types.memory.scope import MemoryScope
    from agent.types.memory.source import TurnRecord
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="u1")
    session = Session()
    user = session.add_message(Message(role="user", content_text="读取这份受保护的诊断资料。"))
    session.add_message(build_assistant_message("", [ToolCallRequest(id="provider", name="read", arguments={})]))
    record = ToolCall(tool_call_id="fact", provider_tool_call_id="provider", session_id=session.session_id,
                      tool_name="read", status="error", error="readonly denied", arguments={})
    session.tool_calls.append(record)
    session.add_message(build_tool_message(record))
    turn = TurnRecord(turn_id="turn", session_id=session.session_id, scope=scope, user_message_id=user.message_id,
                      assistant_message_ids=(), tool_call_ids=("fact",), started_at=1, finished_at=2,
                      completed=True, interrupted=False, error=None)
    storage.sessions.commit_turn(session, turn, scope=scope)
    retriever = MemoryRetriever(RuntimeStorage.open(tmp_path / "agent.db").db)
    assert [hit.reference for hit in retriever.search(scope, "readonly denied", source="raw")] == ["tool:fact"]
    assert retriever.read(scope, "tool:fact").content == "readonly denied"
    assert retriever.read(MemoryScope(user_id="u2"), "tool:fact") is None
