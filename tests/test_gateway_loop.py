"""
Gateway / AgentLoop / Tool / Skill 全链路测试

用 MockModel 替代真实 LLM，覆盖：
    1. session_key 构建公式
    2. DictSessionStore 上下文寻找 / 重开（与 SqliteSessionStore 同接口）
    3. 消息构建函数（user / assistant+tool_calls / tool）
    4. calculator 工具（含安全拦截）
    5. SkillRegistry + use_skill 工具
    6. PrecisionBuilder 的 OpenAI 协议映射
    7. AgentLoop 多步工具循环（mock：先调工具再给文本）
    8. AgentLoop 容错（模型异常 → 重试 → error；超迭代 → 兜底）
    9. Gateway 总线回路（ask 同步问答、/new 短路、同 key 串行）

运行：.venv/Scripts/python.exe -m pytest tests/test_gateway_loop.py -v
"""
from __future__ import annotations
from agent.types.tools import ToolInvocationContext

import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

# 保证从项目根可 import agent 包（pytest 从任意目录运行都行）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.event_bus import EventBus
from tests.loop_fakes import make_loop
from agent.core.building_messages import (
    build_assistant_message,
    build_tool_message,
    build_user_message,
)
from agent.types.memory.scope import MemoryScope
from agent.types.channel import InboundEvent, SessionSource
from agent.types.conversation import Session, ToolCall, TurnResult
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.gateway.session_store import DictSessionStore
from agent.gateway.gateway import Gateway
from agent.gateway.session_key import build_session_key
from agent.model.baseStructure import BaseModel
from agent.types.model import ModelResponse, ToolCallRequest
from agent.skills.registry import SkillRegistry
from agent.skills.skill_tool import UseSkillTool
from agent.tools.calculator_tools import CalculatorTool
from agent.tools.registry import ToolRegistry


# ================= Mock 模型 =================
class MockModel(BaseModel):
    """按脚本返回的假模型：每次 chat 弹出下一个 ModelResponse。"""

    name = "mock"

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []  # 记录每次收到的 messages/tools，供断言

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_event(text: str, channel: str = "cli", chat_id: str = "local") -> InboundEvent:
    return InboundEvent(
        text=text,
        source=SessionSource(channel=channel, chat_id=chat_id, chat_type="dm", user_id="u1"),
    )


# ================= 1. session_key =================
def test_session_key_dm():
    src = SessionSource(channel="cli", chat_id="local", chat_type="dm", user_id="u1")
    assert build_session_key(src, agent_id="main") == "agent:main:cli:dm:local:user:u1"


def test_session_key_dm_maps_to_one_memory_scope():
    """同一 DM chat 下不同 user 必须落在不同槽位：DM scope 按 user 归属。"""
    a = SessionSource(channel="frontier", chat_id="web", chat_type="dm", user_id="alice")
    b = SessionSource(channel="frontier", chat_id="web", chat_type="dm", user_id="bob")
    assert build_session_key(a) != build_session_key(b)


def test_session_key_group_with_user():
    src = SessionSource(channel="feishu", chat_id="oc_x", chat_type="group", user_id="ou_y")
    assert build_session_key(src) == "agent:main:feishu:group:oc_x:user:ou_y"


def test_session_key_thread():
    src = SessionSource(channel="cli", chat_id="c1", chat_type="dm", thread_id="t9")
    assert build_session_key(src) == "agent:main:cli:dm:c1:thread:t9"


# ================= 2. DictSessionStore =================
_STORE_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


def test_store_get_or_create_and_find():
    store = DictSessionStore()
    s1 = store.get_or_create("k1", scope=_STORE_SCOPE, channel="cli", author_id="u1")
    assert store.find("k1") is s1
    assert store.get_or_create("k1", scope=_STORE_SCOPE) is s1  # 同 key 复用上下文
    assert store.find("不存在") is None


def test_store_rejects_scope_change_on_same_key():
    store = DictSessionStore()
    store.get_or_create("k1", scope=_STORE_SCOPE)
    with pytest.raises(ValueError):
        store.get_or_create("k1", scope=MemoryScope(tenant_id="cli", user_id="u2"))


def test_store_close_and_reset_new_session_id():
    store = DictSessionStore()
    s1 = store.get_or_create("k1", scope=_STORE_SCOPE)
    reset = store.close_and_reset("k1", s1, scope=_STORE_SCOPE)
    assert reset.closed_session.session_id == s1.session_id
    assert reset.closed_session.status == "completed"
    assert reset.new_session.session_id != s1.session_id
    assert store.find("k1") is reset.new_session


# ================= 3. 消息构建函数 =================
def test_build_user_message():
    msg = build_user_message(make_event("你好"))
    assert msg.role == "user" and msg.content_text == "你好"
    assert msg.author_id == "u1"


def test_build_assistant_message_with_tool_calls():
    req = ToolCallRequest(id="call_1", name="calculator", arguments={"expression": "1+1"}, arguments_raw='{"expression":"1+1"}')
    msg = build_assistant_message("", [req])
    assert msg.role == "assistant"
    assert msg.metadata["tool_calls"][0]["id"] == "call_1"


def test_build_tool_message():
    record = ToolCall(tool_call_id="call_1", tool_name="calculator", status="success", result_text="1+1 = 2")
    msg = build_tool_message(record)
    assert msg.role == "tool"
    assert msg.metadata["tool_call_id"] == "call_1"
    assert "2" in msg.content_text


# ================= 4. calculator 工具 =================
def test_calculator_basic():
    tool = CalculatorTool()
    r = tool.execute({"expression": "(3+5)*2"}, context=ToolInvocationContext())
    assert r.ok and r.data["value"] == 16


def test_calculator_functions():
    tool = CalculatorTool()
    assert tool.execute({"expression": "sqrt(16)+2**3"}, context=ToolInvocationContext()).data["value"] == 12
    assert tool.execute({"expression": "max(1,9,3)"}, context=ToolInvocationContext()).data["value"] == 9


def test_calculator_rejects_injection():
    tool = CalculatorTool()
    r = tool.execute({"expression": "__import__('os').system('echo hi')"}, context=ToolInvocationContext())
    assert not r.ok  # 白名单外节点必须被拒绝


def test_calculator_bad_expression():
    tool = CalculatorTool()
    assert not tool.execute({"expression": "1+++"}, context=ToolInvocationContext()).ok
    assert not tool.execute({}, context=ToolInvocationContext()).ok


# ================= 5. Skill 体系 =================
def test_skill_registry_loads_builtin():
    registry = SkillRegistry()
    assert "weekly_report" in registry.list_skills()
    summary = registry.summaries_prompt()
    assert "weekly_report" in summary and "use_skill" in summary


def test_use_skill_tool():
    registry = SkillRegistry()
    tool = UseSkillTool(registry)
    r = tool.execute({"skill_name": "weekly_report"}, context=ToolInvocationContext())
    assert r.ok and "周报" in r.result_text
    assert not tool.execute({"skill_name": "不存在"}, context=ToolInvocationContext()).ok


# ================= 6. PrecisionBuilder 协议映射 =================
def test_precision_engine_maps_tool_messages():
    session = Session(session_key="k")
    session.add_message(build_user_message(make_event("算一下 1+1")))
    req = ToolCallRequest(id="call_1", name="calculator", arguments={"expression": "1+1"}, arguments_raw='{"expression": "1+1"}')
    session.add_message(build_assistant_message("", [req]))
    record = ToolCall(tool_call_id="call_1", tool_name="calculator", status="success", result_text="1+1 = 2")
    session.add_message(build_tool_message(record))

    messages = PrecisionBuilder().build(session).messages
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assistant_msg = messages[2]
    assert assistant_msg["tool_calls"][0]["id"] == "call_1"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "calculator"
    assert messages[3]["tool_call_id"] == "call_1"


def test_precision_engine_injects_skill_summary_and_ephemeral():
    session = Session(session_key="k")
    session.add_message(build_user_message(make_event("hi")))
    selection = SkillRegistry().select()
    messages = PrecisionBuilder().build(
        session,
        skill_selection=selection,
        ephemeral_prompt="你在测试环境",
    ).messages
    systems = [m["content"] for m in messages if m["role"] == "system"]
    assert any("weekly_report" in s for s in systems)   # 技能摘要注入
    assert any("测试环境" in s for s in systems)          # ephemeral 注入
    assert messages[-1] == {"role": "user", "content": "hi"}


# ================= 7. AgentLoop 多步工具循环 =================
def _make_loop(script) -> tuple[AgentLoop, EventBus]:
    tools = ToolRegistry()
    tools.register(CalculatorTool())
    tools.register(UseSkillTool(SkillRegistry()))
    bus = EventBus()
    loop = make_loop(
        model=MockModel(script),
        tools=tools,
        skills=SkillRegistry(),
        event_bus=bus,
    )
    return loop, bus


def test_loop_tool_call_then_final_text():
    script = [
        # 第一步：模型要求调 calculator
        ModelResponse(text="", tool_calls=[
            ToolCallRequest(id="call_1", name="calculator", arguments={"expression": "(3+5)*2"}, arguments_raw='{"expression":"(3+5)*2"}')
        ]),
        # 第二步：模型看到工具结果，给出最终文本
        ModelResponse(text="结果是 16"),
    ]
    loop, bus = _make_loop(script)
    q = bus.subscribe()
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("算一下 (3+5)*2")))

    assert result.completed and result.final_text == "结果是 16"
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "16" in session.messages[2].content_text  # 工具结果已回注
    assert len(session.tool_calls) == 1 and session.tool_calls[0].ok

    # 第二次调模型时，上下文里必须带着 tool 消息（协议完整性）
    second_call_messages = loop.model.calls[1]["messages"]
    assert any(m["role"] == "tool" for m in second_call_messages)

    # 事件流完整：turn_start → llm_request → llm_response → tool_call_start → tool_call_result → ... → turn_end
    types = []
    while not q.empty():
        types.append(q.get_nowait().type)
    assert types[0] == "turn_start" and types[-1] == "turn_end"
    assert "tool_call_start" in types and "tool_call_result" in types


def test_loop_skill_call_flow():
    script = [
        ModelResponse(text="", tool_calls=[
            ToolCallRequest(id="call_s", name="use_skill", arguments={"skill_name": "weekly_report"}, arguments_raw='{"skill_name":"weekly_report"}')
        ]),
        ModelResponse(text="# 本周工作周报\n..."),
    ]
    loop, _ = _make_loop(script)
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("帮我写周报")))
    assert result.completed
    assert "周报" in session.messages[2].content_text  # 技能正文已回注


def test_loop_plain_text_single_step():
    loop, _ = _make_loop([ModelResponse(text="你好呀")])
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("hi")))
    assert result.completed and result.final_text == "你好呀"
    assert len(loop.model.calls) == 1


# ================= 8. AgentLoop 容错 =================
def test_loop_model_error_retries_then_fails():
    loop, _ = _make_loop([RuntimeError("网络抖动"), RuntimeError("还是失败"), RuntimeError("彻底失败")])
    loop.model_retries = 2
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("hi")))
    assert not result.completed and "模型调用失败" in result.error
    assert len(loop.model.calls) == 3  # 1 + 2 次重试


def test_loop_model_recovers_on_retry():
    loop, _ = _make_loop([RuntimeError("抖动"), ModelResponse(text="恢复成功")])
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("hi")))
    assert result.completed and result.final_text == "恢复成功"


def test_loop_tool_error_fed_back_not_crash():
    script = [
        ModelResponse(text="", tool_calls=[
            ToolCallRequest(id="c1", name="不存在的工具", arguments={}, arguments_raw="{}")
        ]),
        ModelResponse(text="工具不存在，我直接回答"),
    ]
    loop, _ = _make_loop(script)
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("hi")))
    assert result.completed
    assert session.messages[2].role == "tool"
    assert "失败" in session.messages[2].content_text  # 错误已回注给模型


def test_loop_max_iterations_fallback():
    # 模型永远要求调工具 → 触达上限 → 兜底文案
    infinite_calls = [
        ModelResponse(text="", tool_calls=[
            ToolCallRequest(id=f"c{i}", name="calculator", arguments={"expression": "1"}, arguments_raw='{"expression":"1"}')
        ])
        for i in range(10)
    ]
    loop, _ = _make_loop(infinite_calls)
    loop.max_iterations = 3
    session = Session(session_key="k")
    result = loop.run_turn(session, build_user_message(make_event("hi")))
    assert not result.completed and result.error == "max_iterations"
    assert result.final_text  # 兜底文案非空


# ================= 9. Gateway 总线回路 =================
def _make_gateway(script) -> Gateway:
    loop, _ = _make_loop(script)
    from agent.channel.channel_cli import CliChannel
    gateway = Gateway(loop=loop, store=DictSessionStore())
    gateway.register_channel(CliChannel())
    gateway.start()
    return gateway


def test_gateway_ask_roundtrip():
    gateway = _make_gateway([ModelResponse(text="pong")])
    try:
        reply = gateway.ask(make_event("ping"), timeout=10)
        assert reply.text == "pong"
        # 会话已登记
        key = build_session_key(make_event("ping").source)
        assert gateway.store.find(key) is not None
    finally:
        gateway.stop()


def test_gateway_new_command_shortcuts_loop():
    gateway = _make_gateway([ModelResponse(text="不应被用到")])
    try:
        reply = gateway.ask(make_event("/new"), timeout=10)
        assert "已新开会话" in reply.text
        assert len(gateway.loop.model.calls) == 0  # 斜杠命令不进 Loop
    finally:
        gateway.stop()


def test_gateway_context_accumulates_across_turns():
    gateway = _make_gateway([ModelResponse(text="回复1"), ModelResponse(text="回复2")])
    try:
        gateway.ask(make_event("第一句"), timeout=10)
        gateway.ask(make_event("第二句"), timeout=10)
        key = build_session_key(make_event("x").source)
        session = gateway.store.find(key)
        # user/assistant 各两条，上下文在累积
        assert [m.role for m in session.messages] == ["user", "assistant", "user", "assistant"]
    finally:
        gateway.stop()


def test_gateway_same_session_key_serialized():
    """同 session_key 的两条消息必须串行（第二条看到第一条的完整上下文）。"""
    gateway = _make_gateway([ModelResponse(text="r1"), ModelResponse(text="r2")])
    try:
        import threading
        replies = []
        t1 = threading.Thread(target=lambda: replies.append(gateway.ask(make_event("A"), timeout=10)))
        t2 = threading.Thread(target=lambda: replies.append(gateway.ask(make_event("B"), timeout=10)))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert len(replies) == 2
        key = build_session_key(make_event("x").source)
        session = gateway.store.find(key)
        assert session.message_count == 4  # 两轮完整落进同一 session，无交叉
    finally:
        gateway.stop()


class _ScriptedLoop:
    """按文本回显的 Loop；gated 文本会卡住，直到测试放行。"""

    agent_id = "main"
    action = None
    model = None
    tools = None
    skills = None

    def __init__(self, *, gated: str | None = None, delay: float = 0.0):
        self.gated = gated
        self.delay = delay
        self.entered = threading.Event()
        self.release = threading.Event()
        self.order: list[str] = []
        self._lock = threading.Lock()

    def run_turn(self, session, user_message, **kwargs):
        user_message.trajectory_id = uuid.uuid4().hex[:12]
        session.add_message(user_message)
        if user_message.content_text == self.gated:
            self.entered.set()
            self.release.wait(5)
        time.sleep(self.delay)
        with self._lock:
            self.order.append(user_message.content_text)
        return TurnResult(
            final_text=f"reply:{user_message.content_text}",
            session_id=session.session_id,
            turn_id=user_message.trajectory_id,
        )


def _user_event(text: str, user: str) -> InboundEvent:
    return InboundEvent(
        text=text, source=SessionSource(channel="cli", chat_id=user, chat_type="dm", user_id=user),
    )


def _start(loop, *, workers: int) -> Gateway:
    from agent.channel.channel_cli import CliChannel
    gateway = Gateway(loop=loop, workers=workers)
    gateway.register_channel(CliChannel())
    gateway.start()
    return gateway


def test_concurrent_ask_each_caller_gets_its_own_reply():
    gateway = _start(_ScriptedLoop(delay=0.05), workers=4)
    replies: dict[str, str] = {}

    def ask(user: str) -> None:
        replies[user] = gateway.ask(_user_event(user, user), timeout=5).text

    try:
        threads = [threading.Thread(target=ask, args=(f"user{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        gateway.stop()
    assert replies == {f"user{i}": f"reply:user{i}" for i in range(6)}


def test_queued_turn_does_not_block_other_sessions_and_keeps_order():
    loop = _ScriptedLoop(gated="A1")
    gateway = _start(loop, workers=2)
    replies: dict[str, str] = {}

    def ask(text: str, user: str) -> None:
        replies[text] = gateway.ask(_user_event(text, user), timeout=5).text

    try:
        first = threading.Thread(target=ask, args=("A1", "alice"))
        first.start()
        assert loop.entered.wait(2)
        second = threading.Thread(target=ask, args=("A2", "alice"))
        second.start()
        deadline = time.time() + 2
        while gateway.status()["turns"]["queued"] < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert gateway.status()["turns"]["queued"] == 1

        other = threading.Thread(target=ask, args=("B", "bob"))
        other.start()
        other.join(3)
        assert replies.get("B") == "reply:B"  # alice 排队的消息不占 worker

        loop.release.set()
        first.join(5)
        second.join(5)
    finally:
        loop.release.set()
        gateway.stop()
    assert loop.order == ["B", "A1", "A2"]
    assert replies["A2"] == "reply:A2"


# ================= 10. ClaudeModel 协议转换（纯本地，不发请求） =================
from agent.model.claude import ClaudeModel


def test_claude_convert_messages_basic():
    system, msgs = ClaudeModel._convert_messages([
        {"role": "system", "content": "你是助手"},
        {"role": "system", "content": "你在测试环境"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ])
    assert system == "你是助手\n\n你在测试环境"  # 多条 system 合并
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]


def test_claude_convert_messages_tool_flow():
    system, msgs = ClaudeModel._convert_messages([
        {"role": "user", "content": "算 1+1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "1+1 = 2"},
        {"role": "tool", "tool_call_id": "call_2", "content": "3+3 = 6"},
    ])
    # assistant 的 tool_calls → tool_use 块
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[1]["content"][0]["input"] == {"expression": "1+1"}
    # 相邻 tool 消息合并成一条 user 消息（Anthropic 协议要求）
    assert msgs[2]["role"] == "user"
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["call_1", "call_2"]


def test_claude_convert_tools_schema():
    tools = ClaudeModel._convert_tools([
        {"type": "function", "function": {
            "name": "calculator", "description": "算数",
            "parameters": {"type": "object", "properties": {}},
        }}
    ])
    assert tools[0]["name"] == "calculator"
    assert "input_schema" in tools[0]  # Anthropic 用 input_schema 而非 parameters


def test_claude_parse_response():
    payload = {
        "content": [
            {"type": "text", "text": "我来算一下"},
            {"type": "tool_use", "id": "t1", "name": "calculator", "input": {"expression": "2*3"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp = ClaudeModel._parse_response(payload)
    assert resp.text == "我来算一下"
    assert resp.has_tool_calls and resp.tool_calls[0].name == "calculator"
    assert resp.tool_calls[0].arguments == {"expression": "2*3"}
    assert resp.finish_reason == "tool_calls"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
