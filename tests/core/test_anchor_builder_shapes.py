import json
import pytest
from agent.core.perception.perception_anchor import _parse_json_object, validate_anchor_content
from agent.core.perception.errors import AnchorBuildError


def body(intent):
    return dict(intent=intent,changes=[],decisions=[],next_steps=[],excluded=[])


def test_single_fact_and_named_groups_keep_provenance_when_canonicalized():
    fact=dict(text="Current configuration",source_message_ids=["real"],status="active")
    assert _parse_json_object(json.dumps(body(fact)))["intent"]==[fact]
    assert _parse_json_object(json.dumps(body({"context":[fact],"known_state":[fact]})))["intent"]==[fact,fact]


def test_shape_normalization_never_accepts_unknown_sources_or_string_facts():
    fact=dict(text="invented",source_message_ids=["unknown"],status="active")
    normalized=_parse_json_object(json.dumps(body({"context":[fact]})))
    with pytest.raises(AnchorBuildError) as caught:
        validate_anchor_content(normalized,allowed_ids={"real"},coverage_turns=[])
    assert caught.value.code=="anchor_source_invalid"
    with pytest.raises((ValueError,AnchorBuildError)):
        normalized=_parse_json_object(json.dumps(body({"context":"invented"})))
        validate_anchor_content(normalized,allowed_ids={"real"},coverage_turns=[])
