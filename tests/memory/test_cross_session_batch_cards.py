from __future__ import annotations

import json

from agent.core.memory.memory_extractor import MemoryExtractor
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.storage.sqlite.runtime_storage import RuntimeStorage


class _Model:
    def __init__(self):
        self.batch_sources = []

    def chat(self, messages):
        sources = json.loads(messages[-1]["content"])["sources"]
        self.batch_sources.append(tuple(source["turn_id"] for source in sources))
        if len(sources) == 1:
            return {"text": "[]"}
        source = sources[-1]
        return {"text": json.dumps([{
            "kind": "ongoing_fact", "subject": "DemoCursor", "attribute": "python_version",
            "summary": "DemoCursor 使用 Python 3.11",
            "content": "DemoCursor 使用 Python 3.11",
            "confidence": 0.8, "source_message_ids": [source["user_message_id"]],
            "evidence_quotes": {source["user_message_id"]: source["user_text"]},
            "reason_code": "project_version",
        }], ensure_ascii=False)}


def _turn(storage, scope, route, index, content):
    session = storage.sessions.get_or_create(route, scope=scope)
    user = session.add_message(Message(
        message_id=f"user-{index}", role="user", content_text=content,
    ))
    turn = TurnRecord(
        turn_id=f"turn-{index}", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=(), tool_call_ids=(),
        started_at=float(index), finished_at=float(index) + 0.1,
        completed=True, interrupted=False, error=None,
    )
    storage.sessions.commit_turn(session, turn, scope=scope)


def test_cross_session_batch_keeps_fixed_source_turns(tmp_path):
    model = _Model()
    storage = RuntimeStorage.open(tmp_path / "agent.db", extractor_version=MemoryExtractor.version)
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    extractor = MemoryExtractor(model, storage.memory)
    worker = MemoryReviewWorker(
        storage.sessions, MemoryBatchReviewer(extractor),
        storage.review_committer, extractor_version=extractor.version,
    )
    _turn(storage, scope, "route-a", 1, "今天先讨论安排")
    assert worker.process_pending() == 1
    _turn(storage, scope, "route-a", 2, "我会继续开发项目")
    _turn(storage, scope, "route-b", 3, "我正在开发 DemoCursor，项目使用 Python 3.11。")
    assert worker.process_pending() == 1
    assert model.batch_sources == [("turn-1",), ("turn-2", "turn-3")]
    cards = storage.memory.search(scope, "", statuses=("active",))
    assert len(cards) == 1
    assert cards[0].evidence[0].source_id == "user-3"

