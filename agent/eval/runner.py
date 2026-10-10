"""EvalRunner：在逐 case 隔离运行时中回放真实 Gateway turn。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid

from ..channel.channel_cli import CliChannel
from ..core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.event_bus import EventBus
from agent.core.precision.percision_anchor import ModelAnchorBuilder
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.core.precision.percision_trace import PrecisionTrace
from agent.core.tool_handler import ToolHandler
from agent.core.tool_dispatch import ToolDispatch
from agent.types.precision.admission import AdmissionPolicy
from agent.types.precision.result import PrecisionConfig
from agent.types.runtime import LoopConfig

from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_validator import MemoryValidator
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.core.memory.memory_scope import build_memory_scope, ensure_scope_dir
from agent.types.channel import InboundEvent, SessionSource
from agent.types.conversation import ToolCall
from agent.skills.selector import SkillSelector
from agent.gateway.gateway import Gateway
from ..gateway.session_key import build_session_key
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from ..tools.memory_tools import MemoryReadTool, MemorySearchTool
from ..tools.memory_edit_tool import MemoryEditTool
from ..tools.registry import ToolRegistry
from agent.types.eval.cases import EvalCase, EvalSuite, MemorySeed, TurnExpectation
from agent.types.eval.reports import (
    EvalCaseResult,
    EvalFailure,
    EvalReport,
    EvalTurnResult,
    ModelCallEvidence,
    ToolCallEvidence,
)


class _RecordingModel:
    """记录 Loop 实际交给模型的 messages，再委托真实或脚本模型。"""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._calls: list[list[dict]] = []
        self._lock = threading.Lock()
        self.name = getattr(delegate, "name", "eval-model")

    def chat(self, messages, tools=None, **kwargs):
        with self._lock:
            self._calls.append(copy.deepcopy(messages))
        return self._delegate.chat(messages, tools=tools, **kwargs)

    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def evidence_since(self, start: int) -> tuple[ModelCallEvidence, ...]:
        with self._lock:
            calls = copy.deepcopy(self._calls[start:])
        result: list[ModelCallEvidence] = []
        for index, messages in enumerate(calls, start=start):
            text = json.dumps(messages, ensure_ascii=False, sort_keys=True)
            memory_text = "\n".join(
                str(message.get("content", ""))
                for message in messages
                if "<memory-context" in str(message.get("content", ""))
            )
            experience_text = "\n".join(
                str(message.get("content", "")) for message in messages
                if "<memory-experiences" in str(message.get("content", ""))
            )
            skill_text = "\n".join(
                str(message.get("content", ""))
                for message in messages
                if "## 可用技能（Skills）" in str(message.get("content", ""))
            )
            result.append(
                ModelCallEvidence(
                    call_index=index,
                    context_text=text,
                    context_chars=len(text),
                    memory_text=memory_text,
                    experience_text=experience_text,
                    skill_text=skill_text,
                )
            )
        return tuple(result)


def _tool_evidence_of(calls: list[ToolCall]) -> tuple[ToolCallEvidence, ...]:
    """把 Session 里已落库的 ToolCall 转成评测证据。"""
    evidence: list[ToolCallEvidence] = []
    for call in calls:
        data = call.tool_call_result or {}
        arguments = call.arguments or {}
        skill_name = str(data.get("skill_name") or arguments.get("skill_name") or "")
        try:
            skill_revision = int(data.get("skill_revision") or 0)
        except (TypeError, ValueError):
            skill_revision = 0
        evidence.append(
            ToolCallEvidence(
                name=call.tool_name,
                ok=call.ok,
                skill_name=skill_name.strip(),
                skill_revision=skill_revision,
                error=call.error or "",
            )
        )
    return tuple(evidence)


class EvalRunner:
    """深模块：run(suite) → Report；每个 case 使用全新运行时。

    model_factory 优先于 model：带内部状态的模型必须逐 case 新建，否则前一个 case
    的状态会泄漏成后一个 case 的「能力」。
    """

    def __init__(
        self,
        *,
        memory_root: Path | str,
        db_path: Path | str,
        model=None,
        model_factory=None,
        skill_registry=None,
        skill_selector=None,
        tool_registry=None,
        context_budget=None,
        token_estimator=None,
        memory_extractor_factory=None,
        ask_timeout: float = 30.0,
    ) -> None:
        if model is None and model_factory is None:
            raise ValueError("EvalRunner 需要 model 或 model_factory")
        self._memory_root = Path(memory_root)
        self._db_path = Path(db_path)
        self._model = model
        self._model_factory = model_factory
        self._ask_timeout = ask_timeout
        self._skills = skill_registry
        self._selector = skill_selector
        self._tools = tool_registry
        self._context_budget = context_budget
        self._token_estimator = token_estimator
        self._memory_extractor_factory = memory_extractor_factory
        if self._skills is not None and self._selector is None:
            self._selector = SkillSelector()
        if self._skills is not None and self._tools is None:
            # 没有 use_skill 就无法证明技能正文真的被加载过
            from ..skills.skill_tool import UseSkillTool
            from ..tools.registry import ToolRegistry

            self._tools = ToolRegistry()
            self._tools.register(UseSkillTool(self._skills))
        self._memory_root.mkdir(parents=True, exist_ok=True)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def run(self, suite: EvalSuite) -> EvalReport:
        run_id = uuid.uuid4().hex
        results = [
            self._run_case(suite.name, run_id, case)
            for case in suite.cases
        ]
        passed_count = sum(1 for r in results if r.passed)
        total = len(results)
        return EvalReport(
            suite_name=suite.name,
            suite_version=suite.version,
            passed=passed_count == total and total > 0,
            passed_count=passed_count,
            total_count=total,
            results=tuple(results),
            suite_fingerprint=suite.fingerprint(),
            environment_fingerprint=self._environment_fingerprint(),
        )

    def _environment_fingerprint(self) -> str:
        """模型身份。技能 status/revision 是回放的自变量，不纳入环境指纹。"""
        name = str(
            getattr(self._model, "name", "")
            or getattr(self._model_factory, "__name__", "")
            or "factory"
        )
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

    def _run_case(
        self,
        suite_name: str,
        run_id: str,
        case: EvalCase,
    ) -> EvalCaseResult:
        case_slot = hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:6]
        short_run_id = run_id[:6]
        case_root = self._memory_root / short_run_id / case_slot
        case_root.mkdir(parents=True, exist_ok=True)
        db_path = self._db_path.with_name(
            f"{self._db_path.stem}-{short_run_id}-{case_slot}{self._db_path.suffix}"
        )
        extractor = (
            self._memory_extractor_factory()
            if self._memory_extractor_factory is not None
            else None
        )
        extractor_version = getattr(extractor, "version", "eval-no-extractor")
        storage = RuntimeStorage.open(db_path, extractor_version=extractor_version)
        store = storage.sessions
        markdown_documents = MarkdownDocuments(case_root, storage.db, policy_paths=(case_root / "POLICY.md",), project_paths=(case_root / "PROJECT.md",))
        retriever = MemoryRetriever(
            storage.db, document_root=case_root, documents=markdown_documents,
            allowed_document_paths=(case_root / "POLICY.md", case_root / "PROJECT.md"),
        )
        memory_context = MemoryContextLoader(
            case_root,
            policy_paths=(case_root / "POLICY.md",),
            project_paths=(case_root / "PROJECT.md",),
            document_registry=storage.documents,
            documents=markdown_documents,
            experience_retriever=retriever,
        )
        tools = ToolRegistry()
        if self._tools is not None:
            for name in self._tools.list_tools():
                tools.register(self._tools.get(name))
        tools.register(MemorySearchTool(retriever))
        tools.register(MemoryReadTool(retriever))
        tools.register(MemoryEditTool(storage.memory, markdown_documents))
        delegate = (
            self._model_factory() if self._model_factory is not None else self._model
        )
        model = _RecordingModel(delegate)
        if self._skills is not None and hasattr(self._skills, "reload"):
            # 每个 case 从磁盘真相重新开始，避免上一 case 的写入影响下一 case
            self._skills.reload()
        events, precision_trace = EventBus(), PrecisionTrace()
        trace = AgentLoopTrace(events, precision_trace)
        handler = ToolHandler(ToolDispatch(trace, registry=tools), registry=tools)
        if self._skills is not None:
            self._skills.selector = self._selector
        precision = PrecisionBuilder(
            trace=precision_trace, anchor_builder=ModelAnchorBuilder(model),
            admission_policy=AdmissionPolicy(readback=tools.get("memory_read") is not None),
            config=PrecisionConfig(budget=self._context_budget, token_estimator=self._token_estimator),
        )
        loop = AgentLoop(model, precision=precision, tool_handler=handler,
                         skills=self._skills, trace=trace, config=LoopConfig())
        on_turn_committed = None
        if extractor is not None:
            reviewer = MemoryBatchReviewer(
                extractor,
                validator=MemoryValidator(),
            )
            review_worker = MemoryReviewWorker(
                store,
                reviewer,
                storage.review_committer,
                extractor_version=extractor_version,
            )

            def _process_review(_turn) -> None:
                review_worker.process_pending()

            on_turn_committed = _process_review
        gateway = Gateway(
            loop=loop,
            store=store,
            memory_context=memory_context,
            workers=1,
            on_turn_committed=on_turn_committed,
            event_bus=events, precision_trace=precision_trace,
        )
        gateway.register_channel(CliChannel())

        source = SessionSource(
            channel=case.channel,
            chat_id=f"eval-{suite_name}-{run_id}-{case.case_id}",
            chat_type="dm",
            user_id=case.user_id or f"case-{run_id}-{case.case_id}",
        )
        session_key = build_session_key(source, agent_id=gateway.agent_id)
        scope = build_memory_scope(source)
        for seed in case.seeds:
            self._apply_seed(case_root, scope, seed)

        turns: list[EvalTurnResult] = []
        gateway.start()
        try:
            for index, eval_turn in enumerate(case.turns):
                call_start = model.call_count()
                prior_tools = _session_tool_count(store, session_key)
                reply = gateway.ask(
                    InboundEvent(text=eval_turn.user_text, source=source),
                    timeout=self._ask_timeout,
                )
                evidence = model.evidence_since(call_start)
                tool_calls = _tool_evidence_of(
                    _session_tool_calls(store, session_key)[prior_tools:]
                )
                metadata = reply.metadata or {}
                completed = (
                    "error" not in metadata and not metadata.get("interrupted", False)
                )
                persistence_succeeded = "persistence_error" not in metadata
                failures = self._check_expectation(
                    eval_turn.expectation,
                    evidence=evidence,
                    tool_calls=tool_calls,
                    reply_text=reply.text or "",
                    completed=completed,
                    persistence_succeeded=persistence_succeeded,
                )
                turns.append(
                    EvalTurnResult(
                        turn_index=index,
                        passed=not failures,
                        failures=tuple(failures),
                        reply_text=reply.text or "",
                        completed=completed,
                        tool_calls=tool_calls,
                        persistence_succeeded=persistence_succeeded,
                        model_calls=evidence,
                    )
                )
        finally:
            gateway.stop()

        return EvalCaseResult(
            case_id=case.case_id,
            passed=all(turn.passed for turn in turns),
            turns=tuple(turns),
        )

    @staticmethod
    def _apply_seed(
        memory_root: Path,
        scope: MemoryScope,
        seed: MemorySeed,
    ) -> None:
        if seed.target == "policy":
            path = memory_root / "POLICY.md"
        elif seed.target == "project":
            path = memory_root / "PROJECT.md"
        else:
            path = ensure_scope_dir(memory_root, scope) / "MEMORY.md"
        # 考卷种子模拟管理员/人工建立文档，不经过 AI 的个人手册编辑接口。
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        content = "\n\n".join(part for part in (previous.strip(), seed.content.strip()) if part)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _check_expectation(
        expectation: TurnExpectation,
        *,
        evidence: tuple[ModelCallEvidence, ...],
        tool_calls: tuple[ToolCallEvidence, ...] = (),
        reply_text: str,
        completed: bool,
        persistence_succeeded: bool,
    ) -> list[EvalFailure]:
        failures: list[EvalFailure] = []
        loaded = {
            call.skill_name for call in tool_calls if call.ok and call.skill_name
        }
        for name in expectation.skill_loaded:
            if name not in loaded:
                failures.append(EvalFailure(code="skill.not_loaded", detail=name))
        for name in expectation.skill_not_loaded:
            if name in loaded:
                failures.append(EvalFailure(code="skill.unexpected_load", detail=name))
        if expectation.max_tool_failures is not None:
            actual = sum(1 for call in tool_calls if not call.ok)
            if actual > expectation.max_tool_failures:
                failures.append(
                    EvalFailure(
                        code="tool.too_many_failures",
                        detail=f"limit={expectation.max_tool_failures} actual={actual}",
                    )
                )
        context = "\n".join(call.context_text for call in evidence)
        memory_text = "\n".join(call.memory_text for call in evidence)
        experience_text = "\n".join(call.experience_text for call in evidence)
        skill_text = "\n".join(call.skill_text for call in evidence)
        for needle in expectation.context_contains:
            if needle not in context:
                failures.append(EvalFailure(code="context.missing", detail=needle))
        for needle in expectation.context_lacks:
            if needle in context:
                failures.append(EvalFailure(code="context.forbidden", detail=needle))
        for needle in expectation.memory_contains:
            if needle not in memory_text:
                failures.append(EvalFailure(code="memory.missing", detail=needle))
        for needle in expectation.memory_lacks:
            if needle in memory_text:
                failures.append(EvalFailure(code="memory.forbidden", detail=needle))
        for needle in expectation.experience_contains:
            if needle not in experience_text:
                failures.append(EvalFailure(code="experience.missing", detail=needle))
        for needle in expectation.experience_lacks:
            if needle in experience_text:
                failures.append(EvalFailure(code="experience.forbidden", detail=needle))
        for needle in expectation.skill_contains:
            if needle not in skill_text:
                failures.append(EvalFailure(code="skill.missing", detail=needle))
        for needle in expectation.skill_lacks:
            if needle in skill_text:
                failures.append(EvalFailure(code="skill.forbidden", detail=needle))
        for needle in expectation.reply_contains:
            if needle not in reply_text:
                failures.append(EvalFailure(code="reply.missing", detail=needle))
        for needle in expectation.reply_lacks:
            if needle in reply_text:
                failures.append(EvalFailure(code="reply.forbidden", detail=needle))
        if expectation.completed is not None and completed is not expectation.completed:
            failures.append(
                EvalFailure(
                    code="turn.completed",
                    detail=f"expected={expectation.completed} actual={completed}",
                )
            )
        if (
            expectation.persistence_succeeded is not None
            and persistence_succeeded is not expectation.persistence_succeeded
        ):
            failures.append(
                EvalFailure(
                    code="turn.persistence",
                    detail=(
                        f"expected={expectation.persistence_succeeded} "
                        f"actual={persistence_succeeded}"
                    ),
                )
            )
        if expectation.max_context_chars is not None:
            actual = max((call.context_chars for call in evidence), default=0)
            if actual > expectation.max_context_chars:
                failures.append(
                    EvalFailure(
                        code="context.too_large",
                        detail=f"limit={expectation.max_context_chars} actual={actual}",
                    )
                )
        return failures


def _session_tool_calls(store, session_key: str) -> list[ToolCall]:
    session = store.find(session_key) if hasattr(store, "find") else None
    return list(getattr(session, "tool_calls", None) or [])


def _session_tool_count(store, session_key: str) -> int:
    return len(_session_tool_calls(store, session_key))
