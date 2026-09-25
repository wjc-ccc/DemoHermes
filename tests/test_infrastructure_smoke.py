import json

from agent.core.event_bus import LoopEvent


def test_loop_event_serializes_with_pydantic_v2():
    payload = json.loads(LoopEvent(type="smoke").model_dump_json())

    assert payload["type"] == "smoke"
