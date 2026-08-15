"""EvalRunner — 固定 suite 回放 → Report（8.15 E1/E3）。

经真实 MemoryManager + Gateway 跑 turn；断言只打公开面
（snapshot / 回复 / completed / skill 摘要）。
不 mock Manager/Gateway；模型与可选 SkillRegistry 由调用方注入。
"""
from __future__ import annotations

from pathlib import Path

from ..channel.channel_cli import CliChannel
from ..core.events import EventBus
from ..core.loop import AgentLoop
from ..core.types import InboundEvent, SessionSource
from ..evolution.skill_selector import SkillSelector
from ..gateway import Gateway
from ..gateway.session_key import build_session_key
from ..memory import (
    BuiltinMemoryRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
    build_memory_scope,
)
from ..memory.episodic.store import SqliteSessionStore
from .types import (
    EvalAssertion,
    EvalCase,
    EvalCaseResult,
    EvalReport,
    EvalSeedMemory,
    EvalSuite,
)


class EvalRunner:
    """深模块：run(suite) → Report；复杂度藏在种子写入与 Gateway 回放里。"""

    def __init__(
        self,
        *,
        memory_root: Path | str,
        db_path: Path | str,
        model,
        skill_registry=None,
        skill_selector=None,
        ask_timeout: float = 30.0,
    ) -> None:
        self._memory_root = Path(memory_root)
        self._db_path = Path(db_path)
        self._model = model
        self._ask_timeout = ask_timeout
        self._skills = skill_registry
        self._selector = skill_selector
        if self._skills is not None and self._selector is None:
            self._selector = SkillSelector()
        self._memory_root.mkdir(parents=True, exist_ok=True)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._memory = MemoryManager(root=self._memory_root)
        self._store = SqliteSessionStore(self._db_path)
        self._loop = AgentLoop(
            model=self._model,
            skill_registry=self._skills,
            event_bus=EventBus(),
        )
        self._gateway = Gateway(
            loop=self._loop,
            store=self._store,
            memory=self._memory,
            workers=1,
            skill_selector=self._selector,
        )
        self._gateway.register_channel(CliChannel(quiet=True))

    def run(self, suite: EvalSuite) -> EvalReport:
        results: list[EvalCaseResult] = []
        self._gateway.start()
        try:
            for case in suite.cases:
                results.append(self._run_case(suite.name, case))
        finally:
            self._gateway.stop()

        passed_count = sum(1 for r in results if r.passed)
        total = len(results)
        return EvalReport(
            suite_name=suite.name,
            passed=passed_count == total and total > 0,
            passed_count=passed_count,
            total_count=total,
            results=tuple(results),
        )

    def _run_case(self, suite_name: str, case: EvalCase) -> EvalCaseResult:
        # DM 的 session_key 按 chat_id；scope 按 user_id——case 间必须二者都唯一，避免 slot 冲突
        source = SessionSource(
            channel="cli",
            chat_id=f"eval-{suite_name}-{case.name}",
            chat_type="dm",
            user_id=f"case-{case.name}",
        )
        scope = build_memory_scope(source)
        for seed in case.seeds:
            self._apply_seed(scope, seed)

        session_key = build_session_key(source)
        session = self._store.get_or_create(
            session_key,
            scope=scope,
            channel=source.channel,
            author_id=source.user_id or "",
        )
        snapshot = self._memory.build_context(
            scope=scope,
            query=case.user_text,
            session_id=session.session_id,
            include_today_daily=False,
            enable_provider_recall=False,
        )
        snapshot_prompt = snapshot.prompt or ""
        skill_summary = ""
        if self._skills is not None:
            skill_summary = self._skills.summaries_prompt(
                query=case.user_text,
                selector=self._selector,
            ) or ""

        event = InboundEvent(text=case.user_text, source=source)
        reply = self._gateway.ask(event, timeout=self._ask_timeout)
        reply_text = reply.text or ""
        meta = reply.metadata or {}
        completed = "error" not in meta and "persistence_error" not in meta

        failures = self._check_assertions(
            case.assertions,
            snapshot_prompt=snapshot_prompt,
            reply_text=reply_text,
            completed=completed,
            skill_summary=skill_summary,
        )
        return EvalCaseResult(
            name=case.name,
            passed=len(failures) == 0,
            failures=tuple(failures),
            reply_text=reply_text,
            completed=completed,
            snapshot_prompt=snapshot_prompt,
            skill_summary=skill_summary,
            skill_summary_chars=len(skill_summary),
        )

    def _apply_seed(self, scope: MemoryScope, seed: EvalSeedMemory) -> None:
        result = self._memory.apply(
            BuiltinMemoryRequest(
                scope=scope,
                target=seed.target,
                action="add",
                content=seed.content,
                provenance=MemoryProvenance(source="user_request"),
            )
        )
        if not result.ok:
            raise RuntimeError(f"eval seed 写入失败: {result.message}")

    @staticmethod
    def _check_assertions(
        assertions: tuple[EvalAssertion, ...],
        *,
        snapshot_prompt: str,
        reply_text: str,
        completed: bool,
        skill_summary: str = "",
    ) -> list[str]:
        failures: list[str] = []
        for a in assertions:
            if a.kind == "snapshot_contains":
                needle = str(a.value)
                if needle not in snapshot_prompt:
                    failures.append(f"snapshot_contains:{needle!r}")
            elif a.kind == "reply_contains":
                needle = str(a.value)
                if needle not in reply_text:
                    failures.append(f"reply_contains:{needle!r}")
            elif a.kind == "completed":
                expected = bool(a.value)
                if completed is not expected:
                    failures.append(
                        f"completed:expected={expected} actual={completed}"
                    )
            elif a.kind == "skill_summary_contains":
                needle = str(a.value)
                if needle not in skill_summary:
                    failures.append(f"skill_summary_contains:{needle!r}")
            elif a.kind == "skill_summary_lacks":
                needle = str(a.value)
                if needle in skill_summary:
                    failures.append(f"skill_summary_lacks:{needle!r}")
            elif a.kind == "max_skill_summary_chars":
                limit = int(a.value)
                if len(skill_summary) > limit:
                    failures.append(
                        f"max_skill_summary_chars:limit={limit} actual={len(skill_summary)}"
                    )
            else:
                failures.append(f"unknown_assertion:{a.kind}")
        return failures
