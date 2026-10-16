"""用只读取可见输入的确定性模型验证生产 Anchor 压缩链路。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import re
from threading import Barrier, Event, Lock

import pytest

from agent.core.perception import perception_anchor as anchor_module
from agent.core.perception.errors import AnchorBuildError
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.types.context import ANCHOR_SECTIONS, ContextBudget
from agent.types.model import ModelResponse
from agent.types.perception.result import PerceptionConfig
from tests.core.test_perception_compress import _history, _PerMessage


SLOTS = tuple(slot for slot, _ in ANCHOR_SECTIONS)


def _body(**values):
    return {slot: values.get(slot, []) for slot in SLOTS}


def _fact(text, sources, status="active"):
    return {"text": text, "source_message_ids": list(sources), "status": status}


def _turns(count=1, *, prefix="session", padding=0):
    turns = []
    for index in range(count):
        turns.append({
            "turn_index": index,
            "start_seq": index * 2,
            "end_seq": index * 2 + 1,
            "messages": [
                {"message_id": f"{prefix}-user-{index}", "seq_id": index * 2,
                 "role": "user", "content": f"TURN {index} SESSION {prefix}\n" + "x" * padding,
                 "tool_calls": [], "is_error": False},
                {"message_id": f"{prefix}-assistant-{index}", "seq_id": index * 2 + 1,
                 "role": "assistant", "content": "ack", "tool_calls": [], "is_error": False},
            ],
        })
    return turns


def _payload(turns, *, existing=None, max_tokens=1024):
    sources = [message["message_id"] for turn in turns for message in turn["messages"]]
    for values in (existing or {}).values():
        for fact in values:
            sources.extend(fact.get("source_message_ids", []))
    return {"new_turns": turns, "existing_anchor": existing,
            "allowed_message_ids": sources,
            "constraints": {"max_tokens": max_tokens,
                            "validation_error_from_previous_attempt": None}}


def _phase(text):
    match = re.search(r"(?m)^phase: (\w+)$", text)
    assert match, text[:200]
    return match[1]


def _sources(text):
    return [
        (match[1], match[2], match[3])
        for match in re.finditer(
            r"### Source (\S+) \| Role (\w+)[^\n]*\n(.*?)\n### End source", text, re.S
        )
    ]


def _visible_facts(text):
    """仅读取实际交给模型的来源占位符和事实文本。"""
    if "## Slot " in text:
        for section in re.split(r"(?m)^## Slot ", text)[1:]:
            slot = section.split(" |", 1)[0]
            for match in re.finditer(
                r"(?m)^Fact (f\d+) \| Status (\w+)\nsource_message_ids: [^\n]*\n"
                r"((?:>[^\n]*(?:\n|$))+)", section
            ):
                value = "\n".join(line.removeprefix("> ") for line in match[3].rstrip().splitlines())
                yield slot, match[1], value, match[2]
        return
    existing = text.split("## Existing anchor", 1)[1].split("## Historical turn", 1)[0]
    for slot in SLOTS:
        match = re.search(rf"(?ms)^{slot}:\n(.*?)(?=^\w+:|\Z)", existing)
        if not match:
            continue
        for item in re.split(r"(?m)^  Item \d+:\n", match[1])[1:]:
            source = re.search(r"(?m)^    source_ref: (f\d+)$", item)
            value = re.search(r"(?m)^    text: (.*)$", item)
            status = re.search(r"(?m)^    status: (\w+)$", item)
            if source and value:
                yield slot, source[1], value[1], status[1] if status else "active"


def _echo(text):
    result = _body()
    for slot, source, value, status in _visible_facts(text):
        result[slot].append(_fact(value, [source], status))
    for source, role, value in _sources(text):
        if role == "user":
            marker = re.search(r"TURN (\d+) SESSION ([\w-]+)", value)
            assert marker
            result["intent"].append(_fact(f"{marker[2]}-turn-{int(marker[1]):03d}", [source]))
    return result


class _Model:
    name = "pipeline-fixture"
    model_name = "deterministic"

    def __init__(self, handler=None):
        self.handler = handler or _echo
        self.calls = []
        self.lock = Lock()

    def chat(self, messages, tools=None, **kwargs):
        text = messages[1]["content"]
        with self.lock:
            self.calls.append({"phase": _phase(text), "text": text,
                               "tools": tools, "options": kwargs})
        output = self.handler(text)
        return ModelResponse(text=json.dumps(output),
                             usage={"prompt_tokens": 17, "completion_tokens": 7})


def _builder(model):
    return ModelAnchorBuilder(model, provider_max_output_tokens=4096)


def test_short_history_uses_one_call_and_restores_message_aliases_without_mutating_payload():
    payload = _payload(_turns(2))
    before = deepcopy(payload)
    model = _Model()
    result = _builder(model)(payload)
    assert [call["phase"] for call in model.calls] == ["compress"]
    assert [fact["source_message_ids"] for fact in result["intent"]] == [
        ["session-user-0"], ["session-user-1"]]
    assert all(re.fullmatch(r"m\d+", source) for source, _, _ in _sources(model.calls[0]["text"]))
    assert "session-user-0" not in model.calls[0]["text"]
    assert payload == before
    assert model.calls[0]["tools"] is None


@pytest.mark.parametrize("count, expected", [
    (21, [8, 6, 4, 2, 1]),
    (30, [8, 8, 7, 4, 2, 1]),
])
def test_oldest_segment_is_capped_at_eight_turns_while_recent_segments_get_smaller(count, expected):
    turns = _turns(count, padding=1000)
    segments = anchor_module._split_anchor_turns(turns)
    assert max(map(len, segments)) <= 8
    assert [len(segment) for segment in segments] == expected


def test_segment_boundaries_from_three_to_one_hundred_are_complete_nonempty_and_nonincreasing():
    for count in range(3, 101):
        turns = _turns(count, padding=6000)
        segments = anchor_module._split_anchor_turns(turns)
        widths = [len(segment) for segment in segments]
        assert all(1 <= width <= 8 for width in widths), (count, widths)
        assert widths == sorted(widths, reverse=True), (count, widths)
        assert [turn for segment in segments for turn in segment] == turns, count


def test_geometric_segments_cover_every_complete_turn_once_and_keep_protocol_groups():
    turns = _turns(30, padding=600)
    turns[15]["messages"][1]["tool_calls"] = [{"id": "call-15", "function": {
        "name": "read_file", "arguments": '{"path":"a.py"}'}}]
    turns[15]["messages"].extend([
        {"message_id": "tool-15", "role": "tool", "content": "file body", "tool_calls": [],
         "is_error": False, "seq_id": 31},
        {"message_id": "after-tool-15", "role": "assistant", "content": "done", "tool_calls": [],
         "is_error": False, "seq_id": 31},
    ])
    before = deepcopy(turns)
    segments = anchor_module._split_anchor_turns(turns)
    assert [len(segment) for segment in segments] == [8, 8, 7, 4, 2, 1]
    assert [turn for segment in segments for turn in segment] == turns
    protocol_turns = [turn for segment in segments for turn in segment if turn["turn_index"] == 15]
    assert len(protocol_turns) == 1 and protocol_turns[0]["messages"] == turns[15]["messages"]
    assert turns == before


@pytest.mark.parametrize("kind", ["unicode", "tool_arguments"])
def test_segmentation_threshold_counts_utf8_content_and_tool_call_json(kind):
    turns = _turns(3)
    if kind == "unicode":
        turns[0]["messages"][0]["content"] = "汉" * 5500
    else:
        turns[0]["messages"][1]["tool_calls"] = [{"id": "call", "function": {
            "name": "write_file", "arguments": json.dumps({"body": "x" * 17_000})}}]
    assert len(anchor_module._split_anchor_turns(turns)) > 1
    assert len(anchor_module._split_anchor_turns(_turns(3))) == 1
    assert len(anchor_module._split_anchor_turns(_turns(2, padding=20_000))) == 1


def test_long_history_extracts_then_merges_by_slot_and_restores_all_sources_in_order():
    turns = _turns(30, padding=600)
    payload = _payload(turns)
    before = deepcopy(payload)
    model = _Model()
    result = _builder(model)(payload)
    extracts = [call for call in model.calls if call["phase"] == "extract"]
    assert sorted(len([source for source in _sources(call["text"]) if source[1] == "user"])
                  for call in extracts) == [1, 2, 4, 7, 8, 8]
    assert len(model.calls) == len(anchor_module._split_anchor_turns(turns)) + 1
    assert model.calls[-1]["phase"] == "merge"
    assert [fact["text"] for fact in result["intent"]] == [f"session-turn-{i:03d}" for i in range(30)]
    assert [fact["source_message_ids"] for fact in result["intent"]] == [
        [f"session-user-{i}"] for i in range(30)]
    assert all(re.fullmatch(r"f\d+", source)
               for _, source, _, _ in _visible_facts(model.calls[-1]["text"]))
    assert "session-user-0" not in model.calls[-1]["text"]
    assert payload == before


def test_message_and_existing_fact_aliases_expand_to_stable_union_across_multiple_compactions():
    existing = _body(intent=[_fact("old agreement", ["old-a", "old-b"])])

    def merge_every_source(text):
        sources = [source for _, source, _, _ in _visible_facts(text)]
        sources.extend(source for source, role, _ in _sources(text) if role == "user")
        return _body(intent=[_fact("merged agreement", sources + sources[:1])])

    model = _Model(merge_every_source)
    builder = _builder(model)
    first = builder(_payload(_turns(prefix="first"), existing=existing))
    second = builder(_payload(_turns(prefix="second"), existing=first))
    assert first["intent"][0]["source_message_ids"] == ["old-a", "old-b", "first-user-0"]
    assert second["intent"][0]["source_message_ids"] == [
        "old-a", "old-b", "first-user-0", "second-user-0"]
    assert "old-a" not in model.calls[0]["text"]
    assert "first-user-0" not in model.calls[1]["text"]


def test_long_merge_unions_existing_and_extracted_provenance_within_each_slot():
    existing = _body(changes=[_fact("old change", ["old-change"], "completed")],
                     excluded=[_fact("old exclusion", ["old-exclusion"])])

    def handler(text):
        result = _echo(text)
        if _phase(text) == "extract":
            source = next(source for source, role, _ in _sources(text) if role == "user")
            result["changes"].append(_fact("new change", [source], "completed"))
        elif _phase(text) == "merge":
            result = _body()
            for slot in SLOTS:
                sources = [source for value_slot, source, _, _ in _visible_facts(text)
                           if value_slot == slot]
                if sources:
                    result[slot] = [_fact(f"merged {slot}", sources + sources[:1],
                                          "completed" if slot == "changes" else "active")]
        return result

    turns = _turns(30, padding=600)
    result = _builder(_Model(handler))(_payload(turns, existing=existing))
    assert result["intent"][0]["source_message_ids"] == [f"session-user-{i}" for i in range(30)]
    assert result["changes"][0]["source_message_ids"] == ["old-change", *(
        f"session-user-{part[0]['turn_index']}" for part in anchor_module._split_anchor_turns(turns))]
    assert result["excluded"][0]["source_message_ids"] == ["old-exclusion"]
    assert result["decisions"] == result["next_steps"] == []


@pytest.mark.parametrize("sources", [[], ["m999999"], ["f999999"], ["session-user-0"]])
def test_untrusted_empty_unknown_and_real_id_references_are_rejected(sources):
    builder = _builder(_Model(lambda text: _body(intent=[_fact("invented", sources)])))
    with pytest.raises(AnchorBuildError) as error:
        builder(_payload(_turns()))
    assert error.value.code == "anchor_source_invalid"


def test_a_map_cannot_cite_a_valid_message_alias_owned_by_another_segment():
    observed = []

    def cross_segment(text):
        if _phase(text) == "extract":
            aliases = [source for source, _, _ in _sources(text)]
            foreign = "m60" if "m60" not in aliases else "m1"
            observed.append((aliases, foreign))
            return _body(intent=[_fact("cross-segment citation", [foreign])])
        return _echo(text)

    with pytest.raises(AnchorBuildError) as error:
        _builder(_Model(cross_segment))(_payload(_turns(30, padding=600)))
    assert error.value.code == "anchor_source_invalid"
    assert observed and all(foreign not in local for local, foreign in observed)


def test_parallel_maps_are_bounded_to_three_and_merge_uses_history_order_not_completion_order():
    turns = _turns(30, padding=600)
    segments = anchor_module._split_anchor_turns(turns)
    later_start = segments[1][0]["turn_index"]
    barrier = Barrier(3, timeout=5)
    later_completed = Event()
    lock = Lock()
    active = peak = entered = 0
    completed = []

    def handler(text):
        nonlocal active, peak, entered
        if _phase(text) != "extract":
            return _echo(text)
        start = min(int(re.search(r"TURN (\d+)", value)[1])
                    for _, role, value in _sources(text) if role == "user")
        with lock:
            active += 1
            peak = max(peak, active)
            entered += 1
            first_three = entered <= 3
        try:
            if first_three:
                barrier.wait()
            if start == 0:
                assert later_completed.wait(5)
            result = _echo(text)
            with lock:
                completed.append(start)
            if start == later_start:
                later_completed.set()
            return result
        finally:
            with lock:
                active -= 1

    model = _Model(handler)
    builder = _builder(model)
    builder.begin_capture()
    result = builder(_payload(turns))
    calls = builder.take_calls()
    assert peak == 3
    assert completed.index(later_start) < completed.index(0)
    assert [fact["text"] for fact in result["intent"]] == [f"session-turn-{i:03d}" for i in range(30)]
    assert len(calls) == len(segments) + 1
    assert [call.attempt for call in calls] == list(range(1, len(calls) + 1))
    assert all(call.purpose == "anchor" and call.input_token_count == 17
               and call.output_token_count == 7 for call in calls)
    assert builder.take_calls() == ()


def test_failed_parallel_maps_still_capture_every_actual_model_call():
    def handler(text):
        if _phase(text) == "extract" and "TURN 0 SESSION" in text:
            raise RuntimeError("map unavailable")
        return _echo(text)

    model = _Model(handler)
    builder = _builder(model)
    turns = _turns(30, padding=600)
    builder.begin_capture()
    with pytest.raises(AnchorBuildError):
        builder(_payload(turns))
    calls = builder.take_calls()
    assert len(calls) == len(model.calls) == len(anchor_module._split_anchor_turns(turns))
    assert any(call.error == "map unavailable" and call.input_token_count is None for call in calls)
    assert any(call.error is None and call.input_token_count == 17 for call in calls)
    assert all(call["phase"] == "extract" for call in model.calls)


def test_a_segment_cannot_omit_a_failed_tool_even_when_other_facts_have_valid_sources():
    turns = _turns(30, padding=600)
    turns[0]["messages"].append({"message_id": "failed-tool", "seq_id": 1,
                                 "role": "tool", "content": "tool failed",
                                 "tool_calls": [], "is_error": True})
    model = _Model()
    with pytest.raises(AnchorBuildError) as error:
        _builder(model)(_payload(turns))
    assert error.value.code == "anchor_error_omitted"
    assert all(call["phase"] == "extract" for call in model.calls)


def test_failed_tool_survives_segment_extraction_and_merge_with_its_real_source():
    turns = _turns(30, padding=600)
    turns[0]["messages"][1].update(content="", tool_calls=[{
        "id": "failed-call", "function": {"name": "read_file", "arguments": "{}"}}])
    turns[0]["messages"].extend([
        {"message_id": "failed-tool", "seq_id": 1, "role": "tool", "content": "tool failed",
         "tool_calls": [], "is_error": True},
        {"message_id": "after-failure", "seq_id": 1, "role": "assistant", "content": "failed",
         "tool_calls": [], "is_error": False},
    ])

    def handler(text):
        result = _echo(text)
        for source, role, _ in _sources(text):
            if role == "tool":
                result["decisions"].append(_fact("operation failed", [source], "failed"))
        return result

    model = _Model(handler)
    result = _builder(model)(_payload(turns))
    assert result["decisions"] == [_fact("operation failed", ["failed-tool"], "failed")]
    assert model.calls[-1]["phase"] == "merge"
    assert "failed-tool" not in model.calls[-1]["text"]


def test_outer_retry_keeps_every_map_and_merge_snapshot_but_reports_two_builder_attempts():
    merge_attempts = 0

    def handler(text):
        nonlocal merge_attempts
        if _phase(text) == "merge":
            merge_attempts += 1
            if merge_attempts == 1:
                raise RuntimeError("merge unavailable")
        return _echo(text)

    session = _history([(f"TURN {index} SESSION session\n" + "x" * 700, "ack")
                        for index in range(30)])
    trace = PerceptionTrace()
    model = _Model(handler)
    result = PerceptionBuilder(
        trace=trace, anchor_builder=_builder(model),
        config=PerceptionConfig(compression_trigger_tokens=8),
    ).build(session, budget=ContextBudget(model_context_tokens=110, reserved_output_tokens=10),
            token_estimator=_PerMessage(), trace_id="pipeline-retry", iteration=1)
    calls = trace.get("pipeline-retry").builds[0].auxiliary_calls
    assert result.anchor_updated and result.metrics.anchor_builder_attempts == 2
    assert merge_attempts == 2
    expected_maps = len(anchor_module._split_anchor_turns(_turns(result.anchor.covered_turn_count, padding=700)))
    expected_calls = 2 * (expected_maps + 1)
    assert len(calls) == len(model.calls) == expected_calls
    assert [call.attempt for call in calls] == list(range(1, expected_calls + 1))
    assert sum(call.error == "merge unavailable" for call in calls) == 1
    assert sum(call.input_token_count == 17 and call.output_token_count == 7 for call in calls) == expected_calls - 1
    assert all(call.purpose == "anchor" for call in calls)


def test_invalid_json_keeps_real_usage_and_failure_in_the_call_capture():
    class InvalidModel:
        def chat(self, messages, tools=None, **kwargs):
            return ModelResponse(text="not json", usage={"prompt_tokens": 19, "completion_tokens": 3})

    builder = _builder(InvalidModel())
    builder.begin_capture()
    with pytest.raises(AnchorBuildError) as error:
        builder(_payload(_turns()))
    calls = builder.take_calls()
    assert error.value.code == "anchor_output_invalid"
    assert len(calls) == 1 and calls[0].error
    assert (calls[0].input_token_count, calls[0].output_token_count) == (19, 3)


def test_call_capture_and_alias_resolution_are_isolated_between_parallel_sessions():
    barrier = Barrier(2, timeout=5)

    def handler(text):
        barrier.wait()
        return _echo(text)

    builder = _builder(_Model(handler))

    def run(prefix):
        builder.begin_capture()
        result = builder(_payload(_turns(prefix=prefix)))
        return result, builder.take_calls()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, prefix) for prefix in ("alpha", "beta")]
        outputs = [future.result(timeout=10) for future in futures]
    for prefix, (result, calls) in zip(("alpha", "beta"), outputs):
        assert result["intent"][0]["source_message_ids"] == [f"{prefix}-user-0"]
        assert len(calls) == 1 and calls[0].attempt == 1
        assert calls[0].input_token_count == 17
    assert builder.take_calls() == ()


def test_map_calls_use_provider_capacity_instead_of_dividing_the_final_semantic_budget():
    model = _Model()
    turns = _turns(30, padding=600)
    _builder(model)(_payload(turns, max_tokens=1024))
    extracts = [call for call in model.calls if call["phase"] == "extract"]
    assert len(extracts) == len(anchor_module._split_anchor_turns(turns))
    assert all(call["options"]["max_tokens"] == 4096 for call in extracts)
    assert all(re.search(r"(?m)^max_tokens: 4096$", call["text"]) for call in extracts)
    assert model.calls[-1]["options"]["max_tokens"] == 1024
