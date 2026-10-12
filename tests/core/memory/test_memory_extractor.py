from __future__ import annotations

import json
from tests.prompt_fakes import memory_output, decode_memory_input

import pytest

from agent.core.memory.memory_extractor import MemoryExtractor, MemoryExtractionError
from agent.types.memory.scope import MemoryScope
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message, Session, ToolCall


class _Cards:
    def search(self, scope, query, *, limit, kind=None, offset=0, as_of_ms=None):
        return ()


class _StoredCards:
    def __init__(self, cards):
        self._cards = tuple(cards)

    def search(self, scope, query, *, limit, kind=None, offset=0, as_of_ms=None):
        rows = [card for card in self._cards if kind is None or card.kind.value == kind]
        return tuple(rows[offset:offset + limit])


def _stored_card(identity, content, *, kind, slot_key=None, summary=None, keywords=()):
    return UserMemoryItem(
        item_id=identity, kind=kind, slot_key=slot_key,
        source_type="memory_extraction", source_ref="m0",
        content=content, summary=summary or content,
        content_hash=content_sha256(content), source_authority="user_explicit",
        keywords=keywords,
    )


def _source(text="以后请用中文回答"):
    scope = MemoryScope(user_id="u1")
    session = Session(session_id="s1")
    session.add_message(Message(message_id="m1", role="user", content_text=text))
    turn = TurnRecord(
        turn_id="t1", session_id="s1", scope=scope, user_message_id="m1",
        assistant_message_ids=(), tool_call_ids=(), started_at=1.0,
        finished_at=2.0, completed=True, interrupted=False, error=None,
    )
    return (turn,), (session,)


def test_empty_model_result_is_valid_without_rule_fallback():
    class Model:
        def chat(self, messages):
            return {"text": memory_output()}

    batch = MemoryExtractor(Model(), _Cards()).extract_batch(*_source())
    assert batch.proposals == ()
    assert batch.issues == ()


def test_model_failure_and_invalid_json_raise_for_job_retry():
    class Broken:
        def chat(self, messages):
            raise RuntimeError("unavailable")

    class Invalid:
        def chat(self, messages):
            return {"text": "{not-json"}

    for model in (Broken(), Invalid(), None):
        with pytest.raises(MemoryExtractionError):
            MemoryExtractor(model, _Cards()).extract_batch(*_source())


def test_valid_card_survives_one_invalid_item():
    class Model:
        def chat(self, messages):
            return {"text": memory_output([
                {"kind": "stable_preference", "subject": "assistant",
                 "attribute": "response_language", "summary": "以后用中文回答",
                 "content": "以后用中文回答", "confidence": 0.8,
                 "source_message_ids": ["m1"],
                 "evidence_quotes": {"m1": "以后请用中文回答"},
                 "reason_code": "explicit_preference"},
                {"kind": "profile", "summary": "missing fact slot"},
            ], ensure_ascii=False)}

    batch = MemoryExtractor(Model(), _Cards()).extract_batch(*_source())
    assert len(batch.proposals) == 1
    assert batch.proposals[0].kind.value == "stable_preference"
    assert batch.proposals[0].source_authority == "model_inferred"
    assert len(batch.issues) == 1


def test_interrupted_turn_is_not_extracted():
    turns, sessions = _source()
    interrupted = turns[0].model_copy(update={"completed": False, "interrupted": True})
    with pytest.raises(ValueError, match="正常完成"):
        MemoryExtractor(None, _Cards()).extract_batch((interrupted,), sessions)


def test_memory_tool_output_cannot_be_reused_as_new_card_evidence():
    class Model:
        def chat(self, messages):
            assert "Role tool" not in messages[-1]["content"]
            return {"text": memory_output([{
                "kind": "ongoing_fact", "subject": "user", "attribute": "project",
                "summary": "用户开发 DemoCursor", "content": "用户开发 DemoCursor",
                "confidence": 0.7, "source_tool_call_ids": ["memory-search-1"],
                "evidence_quotes": {"memory-search-1": "DemoCursor"},
                "reason_code": "copied_memory",
            }], ensure_ascii=False)}

    turns, sessions = _source("请查一下旧记忆")
    sessions[0].tool_calls.append(ToolCall(
        tool_call_id="memory-search-1", session_id="s1", trajectory_id="t1",
        tool_name="memory_search", status="success", result_text="DemoCursor",
    ))
    turn = turns[0].model_copy(update={"tool_call_ids": ("memory-search-1",)})
    batch = MemoryExtractor(Model(), _Cards()).extract_batch((turn,), sessions)
    assert batch.proposals == () and len(batch.issues) == 1


def test_existing_cards_keep_stable_slots_and_batch_overlaps():
    cards = (
        _stored_card("name", "用户希望被称作老王", kind="profile",
                     slot_key="profile:user:display_name", summary="用户希望被称作老王"),
        _stored_card("lang", "以后请用中文回答", kind="stable_preference",
                     slot_key="stable_preference:assistant:response_language",
                     summary="以后请用中文回答"),
        _stored_card("cat", "用户养了一只猫", kind="ongoing_fact",
                     slot_key="ongoing_fact:user:pet", summary="用户养了一只猫"),
        _stored_card("png-fact", "DemoCursor 项目使用 PNG 导出图片", kind="ongoing_fact",
                     slot_key="ongoing_fact:user:export_format",
                     summary="DemoCursor 项目使用 PNG 导出图片"),
        _stored_card("cook", "红烧肉的做法和结果", kind="reusable_experience",
                     summary="红烧肉的做法和结果"),
        _stored_card("png-exp", "图片导出改用 PNG 后成功", kind="reusable_experience",
                     summary="图片导出改用 PNG 后成功"),
    )
    model = _CapturingModel()
    MemoryExtractor(model, _StoredCards(cards)).extract_batch(
        *_source("这次图片导出改用 PNG 格式后成功了"),
    )
    shown = model.payload["existing_cards"]
    assert [card["summary"] for card in shown] == [
        "用户希望被称作老王",
        "以后请用中文回答",
        "图片导出改用 PNG 后成功",
        "DemoCursor 项目使用 PNG 导出图片",
    ]
    assert all(set(card) == {"kind", "slot_key", "summary"} for card in shown)
    assert shown[0]["slot_key"] == "profile:user:display_name"
    assert shown[2]["slot_key"] is None


def test_existing_cards_keep_only_eight_related_overlaps():
    cards = [
        _stored_card("name", "用户希望被称作老王", kind="profile",
                     slot_key="profile:user:display_name"),
    ]
    cards.extend(
        _stored_card(
            f"related-{index:02d}", f"PNG 记录 related-{index:02d}",
            kind="ongoing_fact", slot_key=f"ongoing_fact:user:export_{index:02d}",
            summary=f"PNG 记录 related-{index:02d}",
        )
        for index in range(1, 10)
    )
    model = _CapturingModel()
    MemoryExtractor(model, _StoredCards(cards)).extract_batch(*_source("请看 PNG"))
    summaries = [card["summary"] for card in model.payload["existing_cards"]]
    assert summaries[0] == "用户希望被称作老王"
    assert summaries[1:] == [f"PNG 记录 related-{index:02d}" for index in range(1, 9)]


class _CapturingModel:
    def __init__(self):
        self.payload = None

    def chat(self, messages):
        self.payload = decode_memory_input(messages[-1]["content"])
        return {"text": memory_output()}
