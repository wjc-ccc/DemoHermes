"""条目级上下文准入：通过 PrecisionBuilder / AgentLoop 公开接口验证。"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.loop_fakes import make_loop
from agent.types.context import ContextBudget, AdmissionVerdict
from agent.types.precision.admission import AdmissionPolicy
from agent.core.precision.errors import ContextJudgeError
from agent.types.precision.judge import ContextJudgeRequest, ContextJudgeResult
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.core.precision.percision_judge import SystemOneContextJudge
from agent.core.precision.percision_admission import mark_readback_miss
from agent.types.conversation import Message, Session
from agent.types.model import ModelResponse, ToolCallRequest
from agent.tools.base import Tool
from agent.types.tools import ToolCallResult
from agent.tools.registry import ToolRegistry
from tests.anchor_fakes import DeterministicAnchorBuilder

_BIG = 5000
_PREFIX = "[上下文准入]"


class _ByChars:
    """每 100 字一个 token，便于用字数控制用量。"""

    def estimate_messages(self, messages) -> int:
        """返回整批消息的字数成本。"""
        return sum(
            max(1, len(str(message.get("content") or "")) // 100)
            for message in messages
        )


class _FakeJudge:
    """按调用描述中的关键词返回需要度，并记录每次请求。"""

    id = "fake"

    def __init__(self, needs: dict[str, float] | None = None, *, fail: bool = False):
        """needs: 调用描述子串 -> P(still needed)。"""
        self.needs = needs or {}
        self.fail = fail
        self.calls: list[list[ContextJudgeRequest]] = []

    def judge(self, requests):
        """失败模式抛 ContextJudgeError，否则逐条回答。"""
        self.calls.append(list(requests))
        if self.fail:
            raise ContextJudgeError("timeout")
        return [
            ContextJudgeResult(
                item_id=request.item_id,
                kind="log",
                still_needed=next(
                    (p for key, p in self.needs.items() if key in request.call), 0.95
                ),
            )
            for request in requests
        ]


def _tool_turn(
    session: Session,
    user_text: str,
    *,
    name: str,
    arguments: dict,
    call_id: str,
    result: str,
    error: bool = False,
) -> None:
    """追加一个完整历史轮次：user → tool_call → tool 结果 → assistant。"""
    session.add_message(Message(role="user", content_text=user_text))
    session.add_message(Message(
        role="assistant",
        content_text="",
        metadata={"tool_calls": [{
            "id": call_id, "name": name, "arguments_raw": json.dumps(arguments),
        }]},
    ))
    session.add_message(Message(
        role="tool",
        content_text=f"工具执行失败: {result}" if error else result,
        tool_call_ids=[call_id],
        metadata={"tool_call_id": call_id, "tool_name": name},
    ))
    session.add_message(Message(role="assistant", content_text=f"{user_text} 完成"))


def _body(tag: str, size: int = _BIG) -> str:
    """可辨认首尾的长工具输出。"""
    return f"HEAD-{tag} " + ("x" * size) + f" TAIL-{tag}"


def _build(session, *, mode="active", judge=None, cap=None, usage=None, builder=None):
    """按目标用量反推窗口，再通过唯一 interface 构建上下文。"""
    if usage is not None:
        base = PrecisionBuilder().build(session, token_estimator=_ByChars()).metrics.tokens_used
        cap = int(base / usage)
    budget = (
        ContextBudget(model_context_tokens=cap + 10, reserved_output_tokens=10)
        if cap is not None
        else None
    )
    return PrecisionBuilder(
        anchor_builder=builder or DeterministicAnchorBuilder(),
        context_judge=judge,
        admission_policy=AdmissionPolicy(mode=mode),
    ).build(session, budget=budget, token_estimator=_ByChars(), trace_id="adm")


def _tool_contents(result) -> dict[str, str]:
    """tool_call_id -> 发给模型的工具消息正文。"""
    return {
        message["tool_call_id"]: message["content"]
        for message in result.messages
        if message.get("role") == "tool"
    }


def _three_zone_session() -> Session:
    """三条互不相关的长结果，后续对话不再提及其参数。"""
    session = Session(session_id="s")
    _tool_turn(session, "看看 alpha", name="bash", arguments={"command": "pytest alphakeep"},
               call_id="c_keep", result=_body("keep"))
    _tool_turn(session, "看看 beta", name="bash", arguments={"command": "pytest betatrim"},
               call_id="c_trim", result=_body("trim"))
    _tool_turn(session, "看看 gamma", name="bash", arguments={"command": "pytest gammadefer"},
               call_id="c_defer", result=_body("defer"))
    session.add_message(Message(role="user", content_text="继续"))
    return session


# "pytest" 仍出现在之后的调用里，前两条相关度 0.5：keep≈0.82、trim≈0.34、defer≈0.04。
_THREE_ZONE_NEEDS = {"alphakeep": 0.9, "betatrim": 0.3, "gammadefer": 0.05}


def test_off_mode_is_identical_to_no_admission():
    """off 时不判、不改、不产出定案。"""
    session = _three_zone_session()
    judge = _FakeJudge(_THREE_ZONE_NEEDS)
    baseline = _build(session, mode="off", usage=0.75)
    result = _build(session, mode="off", judge=judge, usage=0.75)

    assert result.messages == baseline.messages
    assert judge.calls == []
    assert result.verdicts == {} and result.verdicts_updated is False


def test_below_first_band_makes_no_new_verdict():
    """用量低于 50% 时不值得判，判官不被调用。"""
    judge = _FakeJudge(_THREE_ZONE_NEEDS)
    result = _build(_three_zone_session(), judge=judge, usage=0.3)

    assert judge.calls == []
    assert result.verdicts == {}
    assert all(not content.startswith(_PREFIX) for content in _tool_contents(result).values())


def test_three_probability_zones_map_to_keep_trim_defer():
    """高需要度保留全文，中间裁剪首尾，低需要度移出并给出读回引用。"""
    session = _three_zone_session()
    result = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)
    contents = _tool_contents(result)

    assert contents["c_keep"] == _body("keep")
    assert contents["c_trim"].startswith("HEAD-trim") and contents["c_trim"].endswith("TAIL-trim")
    assert _PREFIX in contents["c_trim"] and len(contents["c_trim"]) < len(_body("trim"))
    assert contents["c_defer"].startswith(_PREFIX)
    assert "tool:c_defer" in contents["c_defer"]
    actions = {verdict.tool_call_id: verdict.action for verdict in result.verdicts.values()}
    assert actions == {"c_keep": "keep", "c_trim": "trim", "c_defer": "defer"}
    assert result.metrics.admission_trimmed_count == 1
    assert result.metrics.admission_deferred_count == 1
    assert result.metrics.admission_saved_tokens > 0
    users = [m["content"] for m in result.messages if m.get("role") == "user"]
    assert users == ["看看 alpha", "看看 beta", "看看 gamma", "继续"]


def test_readback_hint_degrades_without_memory_read():
    """没有 memory_read 工具时，占位符提示重新执行而不是给引用。"""
    session = _three_zone_session()
    result = PrecisionBuilder(
        context_judge=_FakeJudge(_THREE_ZONE_NEEDS),
        admission_policy=AdmissionPolicy(mode="active", readback=False),
    ).build(
        session,
        budget=ContextBudget(
            model_context_tokens=int(
                PrecisionBuilder().build(session, token_estimator=_ByChars()).metrics.tokens_used
                / 0.75
            ) + 10,
            reserved_output_tokens=10,
        ),
        token_estimator=_ByChars(),
    )
    deferred = _tool_contents(result)["c_defer"]
    assert "memory_read" not in deferred and "重新执行" in deferred


def test_shadow_records_verdicts_without_changing_messages():
    """shadow 产出同样的定案和 Trace，但发给模型的消息与 off 完全一致。"""
    session = _three_zone_session()
    off = _build(session, mode="off", usage=0.75)
    shadow = _build(session, mode="shadow", judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)

    assert shadow.messages == off.messages
    assert shadow.verdicts_updated is True
    assert shadow.metrics.admission_mode == "shadow"
    assert shadow.metrics.admission_deferred_count == 1
    noted = {r.admission_action for r in shadow.records if r.admission_action}
    assert noted == {"keep", "trim", "defer"}


def test_superseded_read_is_deferred_by_rule_without_asking_judge():
    """同一文件之后又被读取：旧结果按规则得 0 分移出，不占判官调用。"""
    session = Session(session_id="s")
    _tool_turn(session, "读 a", name="read_file", arguments={"path": "./pkg/a.py"},
               call_id="old", result=_body("old"))
    _tool_turn(session, "再读 a", name="read_file", arguments={"path": "pkg/a.py"},
               call_id="new", result=_body("new"))
    session.add_message(Message(role="user", content_text="继续"))
    judge = _FakeJudge()

    result = _build(session, judge=judge, usage=0.75)
    contents = _tool_contents(result)
    by_call = {verdict.tool_call_id: verdict for verdict in result.verdicts.values()}

    assert contents["old"].startswith(_PREFIX) and "同一文件之后被再次读取" in contents["old"]
    assert contents["new"] == _body("new")
    assert by_call["old"].source == "rule" and by_call["old"].score == 0.0
    asked = {request.call for batch in judge.calls for request in batch}
    assert all("old" not in call for call in asked)
    assert result.metrics.admission_rule_count == 1


def test_judge_failure_falls_back_to_tool_prior():
    """判官超时不阻断构建，按工具类别先验落定案并计数。"""
    result = _build(_three_zone_session(), judge=_FakeJudge(fail=True), usage=0.75)

    assert result.metrics.admission_judge_failures == 1
    assert all(verdict.source == "prior" for verdict in result.verdicts.values())
    assert all(verdict.kind == "log" for verdict in result.verdicts.values())
    assert all("判官不可用 timeout" in verdict.reason for verdict in result.verdicts.values())


def test_tool_errors_are_never_admitted():
    """报错结果必须原文进入（Anchor 也依赖它进入 failed decisions）。"""
    session = Session(session_id="s")
    _tool_turn(session, "跑一下", name="bash", arguments={"command": "make zeta"},
               call_id="err", result=_body("err"), error=True)
    session.add_message(Message(role="user", content_text="继续"))

    result = _build(session, judge=_FakeJudge({"zeta": 0.0}), usage=0.75)

    assert _tool_contents(result)["err"] == f"工具执行失败: {_body('err')}"
    assert result.verdicts == {} and result.metrics.admission_candidates == 0


def test_verdicts_stick_across_builds_without_rejudging():
    """定案写回 Session 后下一次构建直接复用，不再调用判官。"""
    session = _three_zone_session()
    judge = _FakeJudge(_THREE_ZONE_NEEDS)
    first = _build(session, judge=judge, usage=0.75)
    session.context_verdicts = dict(first.verdicts)

    second = _build(session, judge=judge, usage=0.75)

    assert len(judge.calls) == 1
    assert second.verdicts_updated is False
    assert _tool_contents(second) == _tool_contents(first)


def test_higher_usage_band_escalates_stored_verdict_without_rejudging():
    """用量进入更高档时只按已存分数升级 trim → defer。"""
    session = _three_zone_session()
    judge = _FakeJudge(_THREE_ZONE_NEEDS)
    first = _build(session, judge=judge, usage=0.75)
    session.context_verdicts = dict(first.verdicts)

    escalated = _build(session, judge=judge, usage=0.99)
    by_call = {verdict.tool_call_id: verdict for verdict in escalated.verdicts.values()}

    assert len(judge.calls) == 1
    assert by_call["c_trim"].action == "defer"
    assert escalated.verdicts_updated is True


def test_anchor_built_over_projection_still_validates_against_raw_history():
    """Anchor 吃投影后的省字输入，但 digest 按原文计算，下一轮复用不报错。"""
    session = Session(session_id="s")
    _tool_turn(session, "第一次跑", name="bash", arguments={"command": "pytest repeat"},
               call_id="r1", result=_body("r1"))
    _tool_turn(session, "第二次跑", name="bash", arguments={"command": "pytest repeat"},
               call_id="r2", result=_body("r2"))
    for index in range(8):
        _tool_turn(session, f"第{index}步", name="bash",
                   arguments={"command": f"pytest step{index}"},
                   call_id=f"s{index}", result=_body(f"s{index}"))
    session.add_message(Message(role="user", content_text="继续"))
    builder = DeterministicAnchorBuilder()
    judge = _FakeJudge()

    first = _build(session, judge=judge, usage=0.97, builder=builder)
    assert first.metrics.anchor_status == "created"
    sent = [
        message["content"]
        for turn in builder.requests[0]["new_turns"]
        for message in turn["messages"]
        if message["role"] == "tool"
    ]
    assert sent[0].startswith(_PREFIX)

    session.context_anchor = first.anchor
    session.context_verdicts = dict(first.verdicts)
    second = _build(session, judge=judge, usage=0.97, builder=builder)

    assert second.metrics.anchor_error_code is None
    assert second.metrics.anchor_status in {"reused", "extended"}


def test_readback_miss_pins_full_text():
    """读回被移出的结果即判错：钉住为全文，计入误判数。"""
    session = _three_zone_session()
    first = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)
    verdicts, pinned = mark_readback_miss(first.verdicts, "tool:c_defer")
    session.context_verdicts = verdicts

    second = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)

    assert pinned is not None and pinned.missed and pinned.action == "keep"
    assert _tool_contents(second)["c_defer"] == _body("defer")
    assert second.metrics.admission_missed_count == 1
    assert mark_readback_miss(verdicts, "tool:c_keep")[1] is None
    assert mark_readback_miss(verdicts, "message:c_trim")[1] is None


class _MemoryRead(Tool):
    name = "memory_read"
    parameters = {"type": "object", "properties": {"reference": {"type": "string"}}}

    def execute(self, arguments, *, context):
        return ToolCallResult(ok=True, result_text="原文", readback_reference=arguments.get("reference"))


class _ReadbackModel:
    """第一次读回被移出的结果，拿到后结束。"""

    def chat(self, messages, tools=None):
        if messages[-1].get("role") == "tool":
            return ModelResponse(text="好了")
        return ModelResponse(tool_calls=[ToolCallRequest(
            id="rb", name="memory_read", arguments={"reference": "tool:c_defer"},
        )])


def test_agent_loop_pins_verdict_when_model_reads_back():
    """AgentLoop 在 memory_read 命中被移出的结果时钉住定案并发事件。"""
    from agent.core.event_bus import EventBus

    session = _three_zone_session()
    first = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)
    session.context_verdicts = dict(first.verdicts)
    session.messages.pop()
    registry = ToolRegistry()
    registry.register(_MemoryRead())
    bus = EventBus()
    inbox = bus.subscribe()

    result = make_loop(
        model=_ReadbackModel(), tools=registry, event_bus=bus,
        admission_policy=AdmissionPolicy(mode="active"),
    ).run_turn(session, Message(role="user", content_text="继续"))

    by_call = {verdict.tool_call_id: verdict for verdict in session.context_verdicts.values()}
    events = [inbox.get_nowait().type for _ in range(inbox.qsize())]
    assert result.completed
    assert by_call["c_defer"].missed is True and by_call["c_defer"].action == "keep"
    assert "context_admission_miss" in events


def test_session_round_trip_keeps_valid_verdicts_and_drops_broken():
    """定案随 Session payload 持久化；损坏条目不阻断恢复。"""
    verdict = AdmissionVerdict(message_id="m1", tool_call_id="c1", action="defer", score=0.1)
    restored = Session.model_validate({
        **Session(session_id="s").model_dump(mode="json", exclude={"context_verdicts"}),
        "context_verdicts": {"m1": verdict.model_dump(mode="json"), "m2": {"action": "bogus"}},
    })

    assert restored.context_verdicts == {"m1": verdict}


_ENDPOINT = "https://judge.example/v1/systemone"


def _jev(handler, **kwargs) -> SystemOneContextJudge:
    """用 MockTransport 替换网络；端点与模型显式注入。"""
    options = {"base_url": _ENDPOINT, "model": "judge-model", **kwargs}
    return SystemOneContextJudge(
        api_key="k-test", client=httpx.Client(transport=httpx.MockTransport(handler)), **options
    )


def _request(index: int) -> ContextJudgeRequest:
    return ContextJudgeRequest(
        item_id=f"m{index}", goal="修复失败的测试", call=f"bash pytest t{index}",
        result_head="FAILED ...", result_chars=8000, age_turns=2, calls_since=3,
    )


def test_jev_speaks_system_one_and_parses_answers():
    """一次请求：共享 state，每条 choice + noul 两题；choice 的 other 视为无答案。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"answers": {
            "k1": {"choice": "log", "probabilities": {"log": 0.9}},
            "n1": {"noul": 0.12},
            "k2": {"choice": "other"},
            "n2": {"noul": 1.7},
        }})

    results = _jev(handler).judge([_request(1), _request(2)])
    body = json.loads(seen[0].content)

    assert seen[0].url == _ENDPOINT
    assert seen[0].headers["authorization"] == "Bearer k-test"
    assert body["model"] == "judge-model"
    assert body["state"]["goal"] == "修复失败的测试"
    assert body["state"]["i1"]["call"] == "bash pytest t1"
    assert body["questions"]["n1"]["type"] == "noul"
    assert "other" in body["questions"]["k1"]["criteria"]
    assert results == [
        ContextJudgeResult(item_id="m1", kind="log", still_needed=0.12),
        ContextJudgeResult(item_id="m2", kind=None, still_needed=1.0),
    ]


def test_jev_batches_requests_and_keeps_order():
    """超过 batch_size 时切成多次请求，结果顺序与输入一致。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        state = json.loads(request.content)["state"]
        calls.append(state)
        count = len([key for key in state if key.startswith("i")])
        return httpx.Response(200, json={"answers": {
            f"n{index}": {"noul": 0.5} for index in range(1, count + 1)
        }})

    requests = [_request(index) for index in range(10)]
    results = _jev(handler, batch_size=4).judge(requests)

    assert len(calls) == 3
    assert [result.item_id for result in results] == [request.item_id for request in requests]


def test_system_one_http_errors_become_stable_codes():
    """HTTP 错误转成稳定错误码，信息来自响应体而不含密钥。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ContextJudgeError) as caught:
        _jev(handler).judge([_request(1)])

    assert caught.value.code == "http_401" and caught.value.detail == "bad key"


def test_system_one_requires_injected_endpoint_and_model():
    """Adapter 不带任何默认端点或模型。"""
    with pytest.raises(ValueError):
        SystemOneContextJudge(api_key="k", base_url="", model="m")
    with pytest.raises(ValueError):
        SystemOneContextJudge(api_key="k", base_url=_ENDPOINT, model="")


def test_provider_builds_judge_from_jev_config(monkeypatch):
    """provider 决定端点、模型与超时；没有 key 时不构造判官。"""
    from agent import provider

    monkeypatch.setattr(provider, "JEVMODEL_API_KEY", None)
    assert provider.build_context_judge() is None

    monkeypatch.setattr(provider, "JEVMODEL_API_KEY", "k-test")
    monkeypatch.setattr(provider, "JEV_BASE_URL", provider.JEV_OPENROUTER_BASE_URL)
    monkeypatch.setattr(provider, "JEV_MODEL", "~typesafe/jev-latest")
    judge = provider.build_context_judge()

    assert isinstance(judge, SystemOneContextJudge)
    assert judge.id == "systemone:openrouter.ai:~typesafe/jev-latest"


def test_provider_admission_mode_falls_back_to_shadow(monkeypatch):
    """非法 CONTEXT_ADMISSION_MODE 按 shadow 处理。"""
    from agent import provider

    monkeypatch.setattr(provider, "CONTEXT_ADMISSION_MODE", "bogus")
    assert provider.build_admission_policy().mode == "shadow"
    monkeypatch.setattr(provider, "CONTEXT_ADMISSION_MODE", "active")
    assert provider.build_admission_policy().mode == "active"
