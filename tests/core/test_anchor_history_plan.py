"""首次覆盖、JEV 投影与真实来源预算通过 Perception 接口验证。"""
import json
import re

import pytest

from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.types.context import ContextBudget
from agent.types.model import ModelResponse
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionConfig
from agent.types.conversation import Message, Session
from tests.anchor_fakes import DeterministicAnchorBuilder
from tests.core.test_perception_admission import _ByChars, _FakeJudge, _tool_turn
from tests.core.test_perception_compress import _history, _PerMessage


@pytest.mark.parametrize("keep_recent, covered", [(0, 5), (2, 3)])
def test_first_compression_covers_every_old_turn_without_changing_builder_contract(keep_recent, covered):
    session = _history([(f"user-{i}", f"answer-{i}") for i in range(5)])
    original = session.model_dump(mode="json")
    builder = DeterministicAnchorBuilder()
    result = PerceptionBuilder(
        anchor_builder=builder,
        config=PerceptionConfig(compression_trigger_tokens=10, keep_recent_turns=keep_recent),
    ).build(session, token_estimator=_PerMessage())
    assert result.anchor.covered_turn_count == covered
    assert result.metrics.anchor_builder_attempts == 1
    payload = builder.requests[0]
    assert set(payload) == {"existing_anchor", "new_turns", "allowed_message_ids", "constraints"}
    assert [turn["turn_index"] for turn in payload["new_turns"]] == list(range(covered))
    assert set(payload["constraints"]) == {"max_tokens", "validation_error_from_previous_attempt"}
    users = [message["content"] for message in result.messages if message["role"] == "user"]
    assert users == [*(f"user-{i}" for i in range(covered, 5)), "现在继续"]
    assert session.model_dump(mode="json") == original
    assert set(result.model_dump()) == {
        "messages", "metrics", "records", "exposed_refs", "anchor", "anchor_updated", "verdicts",
        "verdicts_updated", "applied_admission_message_ids", "output_token_limit",
    }


def test_segment_extraction_uses_jev_projection_but_digest_and_sources_keep_originals():
    session = Session(session_id="anchor-jev-segments")
    for index in range(8):
        _tool_turn(session, f"任务 {index} " + "u" * 3500,
                   name="run_tests", arguments={}, call_id=f"jev-{index}",
                   result=f"UNPROJECTED-{index}:" + "z" * 20_000)
    session.add_message(Message(role="user", content_text="开始新阶段"))
    originals = session.model_dump(mode="json")
    calls = []

    class Model:
        def chat(self, messages, tools=None, **kwargs):
            prompt = messages[1]["content"]
            calls.append(prompt)
            if "phase: merge" in prompt:
                refs = re.findall(r"Fact (f\d+) ", prompt)
            else:
                refs = re.findall(r"### Source (m\d+) \| Role user", prompt)
            return ModelResponse(text=json.dumps(dict(
                intent=[dict(text="当前阶段约定", source_message_ids=refs, status="active")],
                changes=[], decisions=[], next_steps=[], excluded=[],
            )))

    builder = ModelAnchorBuilder(Model(), provider_max_output_tokens=4096)
    perception = PerceptionBuilder(
        anchor_builder=builder,
        config=PerceptionConfig(compression_trigger_tokens=200, anchor_reserve_tokens=30),
        admission_policy=AdmissionPolicy(mode="active"), context_judge=_FakeJudge({"": 0.0}),
    )
    first = perception.build(session, token_estimator=_ByChars())
    assert first.metrics.anchor_status == "created"
    extracted = [prompt for prompt in calls if "phase: extract" in prompt]
    assert len(extracted) >= 2
    assert all("[上下文准入]" in prompt for prompt in extracted)
    assert all("UNPROJECTED-" not in prompt for prompt in extracted)
    assert session.model_dump(mode="json") == originals
    assert all(source in {message.message_id for message in session.messages}
               for fact in first.anchor.intent for source in fact.source_message_ids)
    session.context_anchor, session.context_verdicts = first.anchor, first.verdicts
    calls.clear()
    second = PerceptionBuilder(
        anchor_builder=builder,
        config=PerceptionConfig(compression_trigger_tokens=100_000),
        admission_policy=AdmissionPolicy(mode="active"), context_judge=_FakeJudge({"": 0.0}),
    ).build(session, token_estimator=_ByChars())
    assert second.metrics.anchor_status == "reused"
    assert second.metrics.anchor_error_code is None
    assert not calls


def test_existing_anchor_can_refresh_after_rendered_sources_exceed_a_smaller_window():
    session = _history([(f"user-{i}", f"answer-{i}") for i in range(5)])
    created = PerceptionBuilder(
        anchor_builder=DeterministicAnchorBuilder(),
        config=PerceptionConfig(compression_trigger_tokens=1),
    ).build(session, token_estimator=_PerMessage())
    session.context_anchor = created.anchor

    class RenderedCost:
        def estimate_messages(self, messages):
            return sum((len(re.findall(r"message:[0-9a-f-]{36}", message.get("content", ""))) * 10 + 1)
                       if message.get("content", "").startswith("<context-anchor") else 1
                       for message in messages)

    requests = []

    def compact(payload):
        requests.append(payload)
        fact = payload["existing_anchor"]["intent"][0]
        return dict(intent=[fact], changes=[], decisions=[], next_steps=[], excluded=[])

    result = PerceptionBuilder(
        anchor_builder=compact,
        config=PerceptionConfig(compression_trigger_tokens=1000),
    ).build(session, token_estimator=RenderedCost(),
            budget=ContextBudget(model_context_tokens=40, reserved_output_tokens=10))
    assert result.metrics.anchor_status == "extended"
    assert requests[0]["new_turns"] == []
    assert requests[0]["constraints"]["max_tokens"] == 24
    assert result.metrics.tokens_used <= 30
