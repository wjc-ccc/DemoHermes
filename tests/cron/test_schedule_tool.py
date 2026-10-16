"""schedule 工具：只作用于当前 scope，拒绝无法推送的通道，邮件只许所有者订阅。"""
import pytest

from agent.core.cron.jobs import JobStore
from agent.core.cron.schedule_tool import ScheduleTool
from agent.core.gateway.session_key import build_session_key
from agent.core.memory.memory_scope import build_memory_scope
from agent.types.channel import SessionSource
from agent.types.tools import ToolInvocationContext

ALICE = SessionSource(channel="feishu", chat_id="oc_alice", chat_type="dm", user_id="ou_alice")
BOB = SessionSource(channel="feishu", chat_id="oc_bob", chat_type="dm", user_id="ou_bob")


def _ctx(source: SessionSource) -> ToolInvocationContext:
    return ToolInvocationContext(session_key=build_session_key(source), memory_scope=build_memory_scope(source))


@pytest.fixture
def tool(tmp_path):
    return ScheduleTool(JobStore(tmp_path))


def test_add_list_remove_scoped(tool):
    added = tool.execute({"action": "add", "text": "喝水", "every_minutes": 30}, context=_ctx(ALICE))
    assert added.ok, added.error
    job_id = added.data["job"]["job_id"]

    assert tool.execute({"action": "list"}, context=_ctx(BOB)).data["jobs"] == []
    listed = tool.execute({"action": "list"}, context=_ctx(ALICE))
    assert [job["job_id"] for job in listed.data["jobs"]] == [job_id]
    assert "当前时间" in listed.result_text

    assert tool.execute({"action": "remove", "job_id": job_id}, context=_ctx(BOB)).ok is False
    assert tool.execute({"action": "remove", "job_id": job_id}, context=_ctx(ALICE)).ok is True
    assert tool.execute({"action": "list"}, context=_ctx(ALICE)).data["jobs"] == []


@pytest.mark.parametrize("channel", ["frontier", "cli"])
def test_rejects_channels_that_cannot_push(tool, channel):
    source = SessionSource(channel=channel, chat_id="web", chat_type="dm", user_id="u1")
    result = tool.execute({"action": "add", "text": "x", "daily": "09:00"}, context=_ctx(source))
    assert result.ok is False and channel in result.error


def test_requires_exactly_one_timing(tool):
    none = tool.execute({"action": "add", "text": "x"}, context=_ctx(ALICE))
    both = tool.execute({"action": "add", "text": "x", "daily": "09:00", "every_minutes": 5}, context=_ctx(ALICE))
    past = tool.execute({"action": "add", "text": "x", "at": "2000-01-01 00:00"}, context=_ctx(ALICE))
    assert not none.ok and not both.ok and not past.ok


def test_mail_only_for_owner(tool, monkeypatch):
    monkeypatch.setenv("NETEASE_MAIL_OWNER", "ou_alice")
    denied = tool.execute({"action": "add", "kind": "mail", "every_minutes": 10}, context=_ctx(BOB))
    assert denied.ok is False and "ou_bob" in denied.error
    allowed = tool.execute({"action": "add", "kind": "mail", "every_minutes": 10}, context=_ctx(ALICE))
    assert allowed.ok, allowed.error
    assert allowed.data["job"]["kind"] == "mail"


def test_mail_denied_without_owner_config(tool, monkeypatch):
    monkeypatch.delenv("NETEASE_MAIL_OWNER", raising=False)
    result = tool.execute({"action": "add", "kind": "mail", "every_minutes": 10}, context=_ctx(ALICE))
    assert result.ok is False
