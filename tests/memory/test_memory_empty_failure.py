from __future__ import annotations

from agent.core.memory.memory_extractor import MemoryExtractor
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage


def _commit(storage, scope):
    session = storage.sessions.get_or_create("slot", scope=scope)
    user = session.add_message(Message(message_id="m1", role="user",
                                       content_text="你好，今天见面。"))
    storage.sessions.commit_turn(session, TurnRecord(
        turn_id="t1", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=(), tool_call_ids=(),
        started_at=1.0, finished_at=2.0, completed=True,
        interrupted=False, error=None,
    ), scope=scope)


def _worker(storage, model):
    extractor = MemoryExtractor(model, storage.memory)
    return MemoryReviewWorker(
        storage.sessions, MemoryBatchReviewer(extractor),
        storage.review_committer, extractor_version=extractor.version,
        max_attempts=2,
    )


def test_empty_batch_completes_without_card(tmp_path):
    class Empty:
        def chat(self, messages):
            return {"text": '{"cards":[],"skipped":[]}'}

    storage = RuntimeStorage.open(tmp_path / "agent.db", extractor_version=MemoryExtractor.version)
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    _commit(storage, scope)
    assert _worker(storage, Empty()).process_pending() == 1
    assert storage.jobs.list(job_type="memory.extract")[0].status == "completed"
    assert storage.memory.search(scope, "") == ()


def test_model_error_is_retryable_failure_not_empty_success(tmp_path):
    class Broken:
        def chat(self, messages):
            raise RuntimeError("unavailable")

    storage = RuntimeStorage.open(tmp_path / "agent.db", extractor_version=MemoryExtractor.version)
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    _commit(storage, scope)
    assert _worker(storage, Broken()).process_pending() == 1
    job = storage.jobs.list(job_type="memory.extract")[0]
    assert job.status == "retryable_failed"
    assert storage.memory.search(scope, "") == ()

