"""版本化 Memory 案例、历史、预期记忆和黄金问题。

职责：校验案例的时间、来源引用和评分标准，生成不可变内容指纹。
边界：只定义考题，不调用模型、不生成用户记忆、不判断实际回答。
关联：memory_dataset.py 导入和导出；SQLite memory_eval_store.py 保存发布版本。
"""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from agent.types.memory.categories import UserCardKind, ExperienceOutcome


class _Definition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MemoryEvalTurn(_Definition):
    turn_id: str = Field(min_length=1)
    user_text: str = Field(min_length=1)


class MemoryEvalSession(_Definition):
    session_id: str = Field(min_length=1)
    occurred_at: datetime | None = None
    turns: tuple[MemoryEvalTurn, ...] = Field(min_length=1)


class MemoryExpectation(_Definition):
    memory_id: str = Field(min_length=1)
    kind: UserCardKind
    claim: str = Field(min_length=1)
    anchors: tuple[tuple[str, ...], ...] = Field(min_length=1)
    query: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    evidence_turn_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    outcome_status: ExperienceOutcome | None = None


class ForbiddenMemory(_Definition):
    description: str = Field(min_length=1)
    anchors: tuple[tuple[str, ...], ...] = Field(min_length=1)
    kind: UserCardKind | None = None


class MemoryGoldenQuestion(_Definition):
    question_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    asked_at: datetime | None = None
    history_session_count: int | None = Field(default=None, ge=1)
    expected_memory_ids: tuple[str, ...] = ()
    evidence_turn_ids: tuple[str, ...] = ()
    expected_action: Literal["answer", "make_plan", "draft", "lookup_history", "ask_clarification"] = "answer"
    reference_answer: str = Field(min_length=1)
    required_meaning: tuple[str, ...] = Field(min_length=1)
    forbidden_meaning: tuple[str, ...] = ()
    answer_contains: tuple[tuple[str, ...], ...] = ()
    answer_lacks: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    requires_semantic_review: bool = True


class MemoryEvalCase(_Definition):
    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    scenario: str = Field(min_length=1)
    difficulty: Literal["basic", "multi_session"]
    abilities: tuple[str, ...] = Field(min_length=1)
    history: tuple[MemoryEvalSession, ...] = Field(min_length=1)
    expected_memories: tuple[MemoryExpectation, ...] = ()
    ignored_information: tuple[str, ...] = ()
    forbidden_memories: tuple[ForbiddenMemory, ...] = ()
    questions: tuple[MemoryGoldenQuestion, ...] = ()
    strict_card_count: bool = True

    @model_validator(mode="after")
    def check_references(self):
        session_ids = [session.session_id for session in self.history]
        turn_ids = [turn.turn_id for session in self.history for turn in session.turns]
        memory_ids = [memory.memory_id for memory in self.expected_memories]
        question_ids = [question.question_id for question in self.questions]
        for values in (session_ids, turn_ids, memory_ids, question_ids):
            if len(values) != len(set(values)):
                raise ValueError("案例内 Session、Turn、Memory 和 Question ID 各自必须唯一")
        for memory in self.expected_memories:
            if not set(memory.evidence_turn_ids) <= set(turn_ids):
                raise ValueError("预期记忆引用了不存在的原文轮次")
        for question in self.questions:
            count = question.history_session_count or len(self.history)
            if count > len(self.history):
                raise ValueError("问题引用的历史阶段不存在")
            prefix = self.history[:count]
            allowed = {turn.turn_id for session in prefix for turn in session.turns}
            if not set(question.evidence_turn_ids) <= allowed:
                raise ValueError("黄金问题不能引用未来会话或不存在的原文")
            if not set(question.expected_memory_ids) <= set(memory_ids):
                raise ValueError("黄金问题引用了不存在的预期记忆")
            if question.asked_at and any(session.occurred_at and session.occurred_at > question.asked_at
                                         for session in prefix):
                raise ValueError("提问时间早于可读取的历史会话")
        if any(not group or any(not term.strip() for term in group)
               for memory in self.expected_memories for group in memory.anchors):
            raise ValueError("预期记忆的匹配词组不能为空")
        return self


class MemoryEvalDataset(_Definition):
    dataset_id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    schema_version: Literal["1"] = "1"
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source: Literal["synthetic"] = "synthetic"
    created_at: datetime
    cases: tuple[MemoryEvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_dataset(self):
        if self.created_at.tzinfo is None:
            raise ValueError("案例集创建时间必须包含时区")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("同一版本内 case_id 必须唯一")
        for case in self.cases:
            times = [session.occurred_at for session in case.history if session.occurred_at]
            if any(time.tzinfo is None for time in times):
                raise ValueError("会话时间必须包含时区")
            if times != sorted(times):
                raise ValueError("历史会话必须按时间顺序排列")
            for question in case.questions:
                if question.asked_at and question.asked_at.tzinfo is None:
                    raise ValueError("黄金问题的时间必须包含时区")
            if case.difficulty == "multi_session" and (
                not case.questions or len(times) != len(case.history)
                or any(question.asked_at is None for question in case.questions)
            ):
                raise ValueError("日常黄金案例必须提供完整时间和黄金问题")
        return self
