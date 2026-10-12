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
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from agent.types.memory.categories import UserCardKind, ExperienceOutcome


class _Definition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvalToolObservation(_Definition):
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    status: Literal["pending", "running", "success", "error", "timeout", "cancelled"] = "success"
    result_text: str | None = None
    result_data: dict | None = None
    error: str | None = None
    provider_tool_call_id: str | None = None


class MemoryEvalTurn(_Definition):
    turn_id: str = Field(min_length=1)
    user_text: str = Field(min_length=1)
    assistant_text: str | None = None
    tool_results: tuple[EvalToolObservation, ...] = ()

    @model_serializer(mode="wrap")
    def _wire(self, handler):
        result = handler(self)
        if self.assistant_text is None:
            result.pop("assistant_text", None)
        if not self.tool_results:
            result.pop("tool_results", None)
        return result


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
    question_schema_version: Literal["1", "2"] = "1"
    history_end_turn_id: str | None = None
    expected_active_memory_ids: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _contract(self):
        if self.question_schema_version == "2" and not self.history_end_turn_id:
            raise ValueError("新版问题必须声明 history_end_turn_id")
        if self.question_schema_version == "1" and (
            self.history_end_turn_id is not None or self.expected_active_memory_ids is not None
        ):
            raise ValueError("新增问题字段必须使用 question_schema_version=2")
        return self

    @model_serializer(mode="wrap")
    def _wire(self, handler):
        result = handler(self)
        if self.question_schema_version == "1":
            for key in ("question_schema_version", "history_end_turn_id", "expected_active_memory_ids"):
                result.pop(key, None)
        return result


class MemoryEvalCase(_Definition):
    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    scenario: str = Field(min_length=1)
    difficulty: Literal["basic", "multi_session", "long_context", "long_benchmark", "long_reuse"]
    target: Literal["memory", "perception", "combined"] = "memory"
    abilities: tuple[str, ...] = Field(min_length=1)
    history: tuple[MemoryEvalSession, ...] = Field(min_length=1)
    expected_memories: tuple[MemoryExpectation, ...] = ()
    ignored_information: tuple[str, ...] = ()
    forbidden_memories: tuple[ForbiddenMemory, ...] = ()
    questions: tuple[MemoryGoldenQuestion, ...] = ()
    strict_card_count: bool = True

    @model_serializer(mode="wrap")
    def _wire(self, handler):
        result = handler(self)
        if self.target == "memory" and self.difficulty != "long_context":
            result.pop("target", None)
        return result

    def history_for(self, question):
        count = question.history_session_count or len(self.history)
        if count > len(self.history):
            raise ValueError("问题引用的历史阶段不存在")
        prefix = self.history[:count]
        if not question.history_end_turn_id:
            return prefix
        selected = []
        for session in prefix:
            ids = [turn.turn_id for turn in session.turns]
            if question.history_end_turn_id in ids:
                selected.append(session.model_copy(update={"turns": session.turns[:ids.index(question.history_end_turn_id) + 1]}))
                return tuple(selected)
            selected.append(session)
        raise ValueError("历史结束轮次不存在或位于未来会话")

    @model_validator(mode="after")
    def check_references(self):
        session_ids = [session.session_id for session in self.history]
        turn_ids = [turn.turn_id for session in self.history for turn in session.turns]
        memory_ids = [memory.memory_id for memory in self.expected_memories]
        question_ids = [question.question_id for question in self.questions]
        tool_ids = [tool.tool_call_id for session in self.history for turn in session.turns for tool in turn.tool_results]
        for values in (session_ids, turn_ids, memory_ids, question_ids, tool_ids):
            if len(values) != len(set(values)):
                raise ValueError("案例内 Session、Turn、Memory 和 Question ID 各自必须唯一")
        for memory in self.expected_memories:
            if not set(memory.evidence_turn_ids) <= set(turn_ids):
                raise ValueError("预期记忆引用了不存在的原文轮次")
        for question in self.questions:
            prefix = self.history_for(question)
            allowed = {turn.turn_id for session in prefix for turn in session.turns}
            if not set(question.evidence_turn_ids) <= allowed:
                raise ValueError("黄金问题不能引用未来会话或不存在的原文")
            if not set(question.expected_memory_ids) <= set(memory_ids):
                raise ValueError("黄金问题引用了不存在的预期记忆")
            if question.expected_active_memory_ids is not None:
                if not set(question.expected_active_memory_ids) <= set(memory_ids):
                    raise ValueError("检查点引用了不存在的有效记忆")
                selected_memories = [memory for memory in self.expected_memories
                                     if memory.memory_id in question.expected_active_memory_ids]
                if any(not set(memory.evidence_turn_ids) <= allowed for memory in selected_memories):
                    raise ValueError("检查点有效记忆不能依赖未来证据")
            if question.asked_at and any(session.occurred_at and session.occurred_at > question.asked_at
                                         for session in prefix):
                raise ValueError("提问时间早于可读取的历史会话")
        if any(not group or any(not term.strip() for term in group)
               for memory in self.expected_memories for group in memory.anchors):
            raise ValueError("预期记忆的匹配词组不能为空")
        if self.difficulty == "long_context":
            if not 40 <= len(turn_ids) <= 80 or len(self.questions) != 2:
                raise ValueError("长对话案例必须包含40到80轮和两个检查点问题")
            if any(question.question_schema_version != "2" for question in self.questions):
                raise ValueError("长对话问题必须使用字段契约版本2")
        if self.difficulty == "long_benchmark":
            if len(turn_ids) not in {40, 80, 120} or len(self.questions) != 3:
                raise ValueError("核心长对话评测必须为40/80/120轮和三个检查点")
            if any(question.question_schema_version != "2" for question in self.questions):
                raise ValueError("核心长对话问题必须声明字段契约版本2")
        if self.difficulty == "long_reuse" and (len(turn_ids) not in {40,80,120} or len(self.questions)!=6):
            raise ValueError("连续复用案例必须为40/80/120轮和六个连续问题")
        return self


class MemoryEvalDataset(_Definition):
    dataset_id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    schema_version: Literal["1", "2", "3"] = "1"
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
            if case.difficulty in {"multi_session", "long_context", "long_benchmark", "long_reuse"} and (
                not case.questions or len(times) != len(case.history)
                or any(question.asked_at is None for question in case.questions)
            ):
                raise ValueError("日常黄金案例必须提供完整时间和黄金问题")
            if self.schema_version == "1" and (
                case.target != "memory" or case.difficulty == "long_context"
                or any(question.question_schema_version != "1" for question in case.questions)
                or any(turn.tool_results or turn.assistant_text is not None for session in case.history for turn in session.turns)
            ):
                raise ValueError("新增案例字段必须使用schema_version=2")
            if case.difficulty in {"long_benchmark","long_reuse"} and self.schema_version != "3":
                raise ValueError("核心长对话评测必须使用schema_version=3")
        return self


# Generic names for the shared perception/memory/combined definitions.
EvalDataset = MemoryEvalDataset
EvalCase = MemoryEvalCase
EvalTurn = MemoryEvalTurn
EvalSession = MemoryEvalSession
GoldenQuestion = MemoryGoldenQuestion
