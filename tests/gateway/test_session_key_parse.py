"""parse_session_key 必须是 build_session_key 的严格逆运算。"""
import pytest

from agent.core.gateway.session_key import build_session_key, parse_session_key
from agent.types.channel import SessionSource


@pytest.mark.parametrize("source", [
    SessionSource(channel="feishu", chat_id="oc_1", chat_type="dm", user_id="ou_1"),
    SessionSource(channel="feishu", chat_id="oc_2", chat_type="group", user_id="ou_2"),
    SessionSource(channel="wechat", chat_id="wx@im.wechat", chat_type="dm", user_id="wx@im.wechat", thread_id="t1"),
    SessionSource(channel="cli", chat_id="c1", chat_type="dm", thread_id="t9"),
])
def test_round_trip(source):
    key = build_session_key(source, agent_id="main")
    parsed = parse_session_key(key)
    assert parsed is not None
    assert build_session_key(parsed, agent_id="main") == key
    assert (parsed.channel, parsed.chat_id, parsed.chat_type, parsed.user_id, parsed.thread_id) == (
        source.channel, source.chat_id, source.chat_type, source.user_id, source.thread_id)


@pytest.mark.parametrize("key", [
    "",
    "agent:main:feishu",
    "agent:main:feishu:private:oc_1",
    "agent:main:feishu:dm:oc_1:user",
    "agent:main:feishu:dm:oc_1:user:a:user:b",
    "agent:main:feishu:dm:oc:1:user:ou_1",
    "agent:main:feishu:dm:oc_1:thread:t:user:ou_1",
])
def test_ambiguous_or_malformed_key_is_rejected(key):
    assert parse_session_key(key) is None
