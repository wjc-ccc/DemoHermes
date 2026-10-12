from __future__ import annotations

import json
from tests.prompt_fakes import memory_output, decode_memory_input
from types import SimpleNamespace

from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.storage.sqlite.runtime_storage import RuntimeStorage


class _ExperienceModel:
    def chat(self, messages):
        sources = decode_memory_input(messages[-1]["content"])["sources"]
        source_id = sources[0]["user_message_id"]
        return SimpleNamespace(text=memory_output([{
            "kind": "reusable_experience",
            "title": "Windows 项目测试", "summary": "该项目中使用虚拟环境运行 pytest",
            "content": "一次观察：在 Windows 项目运行 pytest 时，使用项目虚拟环境后测试成功。",
            "keywords": ["pytest", "Windows", "Pydantic"],
            "applicability": "在该 Windows 项目运行 pytest 时",
            "action": "先激活项目虚拟环境再运行 pytest",
            "outcome": "该次测试成功",
            "outcome_status": "success",
            "limitations": "只在这个项目观察到一次，其他项目需核对依赖",
            "confidence": 0.8, "source_message_ids": [source_id],
            "evidence_quotes": {source_id: sources[0]["user_text"]},
            "reason_code": "observed_test_environment",
        }], ensure_ascii=False))


def test_completed_turn_extraction_is_recalled_in_new_session(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db", extractor_version=MemoryExtractor.version)
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = storage.sessions.get_or_create("first", scope=scope)
    user = session.add_message(Message(
        message_id="source-user", role="user",
        content_text="在 Windows 项目用虚拟环境运行 pytest，Pydantic 测试通过了。",
    ))
    assistant = session.add_message(Message(
        message_id="source-assistant", role="assistant", content_text="收到。"
    ))
    turn = TurnRecord(
        turn_id="first-turn", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=(assistant.message_id,),
        tool_call_ids=(), started_at=1.0, finished_at=1.1,
        completed=True, interrupted=False, error=None,
    )
    storage.sessions.commit_turn(session, turn, scope=scope)

    extractor = MemoryExtractor(_ExperienceModel(), storage.memory)
    reviewer = MemoryBatchReviewer(extractor)
    worker = MemoryReviewWorker(
        storage.sessions, reviewer, storage.review_committer,
        extractor_version=extractor.version,
    )
    assert worker.process_pending() == 1
    cards = storage.memory.search(scope, "", statuses=("active",))
    assert len(cards) == 1 and cards[0].evidence[0].source_id == user.message_id

    new_session = storage.sessions.get_or_create("second", scope=scope)
    retriever = MemoryRetriever(storage.db)
    loader = MemoryContextLoader(
        tmp_path / "memory", documents=MarkdownDocuments(tmp_path / "memory", storage.db),
        experience_retriever=retriever,
    )
    snapshot = loader.freeze(scope, session_id=new_session.session_id,
                             query="Windows 项目的 pytest 怎么运行？")
    assert snapshot.experience_prompt and "项目虚拟环境" in snapshot.experience_prompt
    assert f"card:{cards[0].item_id}" in snapshot.experience_prompt
    new_session.add_message(Message(role="user", content_text="Windows 项目 pytest 怎么运行？"))
    built = PerceptionBuilder().build(new_session, memory_snapshot=snapshot)
    experience_record = next(record for record in built.records
                             if record.item_name == "memory_experience")
    assert cards[0].item_id in experience_record.content_preview
    assert "命中经验关键词" in experience_record.content_preview
    assert loader.freeze(scope, session_id=new_session.session_id,
                         query="今晚吃什么？").experience_prompt is None
