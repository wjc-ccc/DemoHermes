"""RuleBasedMemoryExtractor 的版本化分层与安全拒写 golden。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core.memory import MemoryScope, RuleBasedMemoryExtractor
from agent.core.memory.types import TurnRecord
from agent.core.types import Message, Session
from agent.eval import ClassificationRequest, ClassificationSample, run_classification

_DATASET_PATH = Path(__file__).parent / "datasets" / "memory_extraction_rule_based.json"
_CASES = tuple(json.loads(_DATASET_PATH.read_text(encoding="utf-8"))["cases"])


def _inputs(case: dict) -> tuple[TurnRecord, Session]:
    scope = MemoryScope(
        tenant_id="cli",
        user_id="eval-extract",
        task_id="task-1" if case["task_scope"] else None,
    )
    message = Message(
        message_id=f"message-{case['id']}",
        role="user",
        content_text=case["text"],
    )
    session = Session(session_id=f"session-{case['id']}", messages=[message])
    turn = TurnRecord(
        turn_id=f"turn-{case['id']}",
        session_id=session.session_id,
        scope=scope,
        user_message_id=message.message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )
    return turn, session


@pytest.mark.parametrize("case", _CASES, ids=[case["id"] for case in _CASES])
def test_rule_extractor_golden(case):
    turn, session = _inputs(case)
    proposals = RuleBasedMemoryExtractor(id_factory=lambda: "proposal-1").extract(
        turn, session
    )
    expected = case["expected_layer"]

    if expected == "none":
        assert proposals == []
        return
    assert len(proposals) == 1
    assert proposals[0].target_layer.value == expected
    assert proposals[0].source_message_ids == (turn.user_message_id,)


def test_rule_extractor_dataset_scorecard():
    samples = []
    for case in _CASES:
        turn, session = _inputs(case)
        proposals = RuleBasedMemoryExtractor().extract(turn, session)
        actual = proposals[0].target_layer.value if proposals else "none"
        samples.append(
            ClassificationSample(
                case_id=case["id"],
                expected_label=case["expected_layer"],
                actual_label=actual,
                tags=tuple(case["tags"]),
            )
        )

    report = run_classification(
        ClassificationRequest(samples=tuple(samples), labels=("user", "task", "none"))
    )
    assert report.accuracy == 1.0
    assert report.macro_f1 == 1.0
    assert report.errors == ()
