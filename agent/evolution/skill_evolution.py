"""SkillEvolution — 技能自进化的唯一生产入口。

commit 之后：从已落库 TurnRecord + Session 抽出确定性事实 → 写 draft 候选 →
把晋升/打补丁交给后台 shadow replay。主回复路径只做廉价的写候选，不跑评测。
"""
from __future__ import annotations

import logging
import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

from ..eval.types import EvalCase, EvalSuite, EvalTurn, TurnExpectation
from ..core.memory import MemoryScope
from ..core.memory.types import TurnRecord
from ..skills.repository import SkillChangeRequest, SkillRepository
from .skill_creator import SkillCreator
from .skill_distiller import TrajectorySkillDistiller
from .skill_gate import SkillLifecycleGate
from .skill_optimizer import SkillOptimizer
from .turn_facts import build_turn_facts
from .types import SkillPatchProposal, SkillProposal

logger = logging.getLogger(__name__)

RunnerFactory = Callable  # (SkillRegistry) -> EvalRunner


def suite_for_skill(
    *,
    skill_name: str,
    user_text: str,
    description: str = "",
    scope: MemoryScope | None = None,
) -> EvalSuite:
    """用沉淀当轮的真实用户任务当最小考题；没有用户原文则无法评测。"""
    prompt = (user_text or "").strip() or (description or "").strip()
    if not prompt:
        raise ValueError("没有可用于回放的用户任务")
    user_id = scope.user_id if scope is not None and scope.user_id else ""
    channel = scope.tenant_id if scope is not None and scope.tenant_id else "cli"
    return EvalSuite(
        name=f"skill_{skill_name}",
        version="origin-1",
        target_skills=(skill_name,),
        cases=(
            EvalCase(
                case_id="origin_task",
                channel=channel,
                user_id=user_id,
                turns=(
                    EvalTurn(
                        user_text=prompt,
                        expectation=TurnExpectation(
                            skill_contains=(skill_name,),
                            completed=True,
                            max_tool_failures=0,
                        ),
                    ),
                ),
                tags=("origin", skill_name),
            ),
        ),
    )


class SkillReplayWorker:
    """后台执行 shadow replay；失败不影响主对话。"""

    def __init__(self, evolve: "SkillEvolution", jobs) -> None:
        self._evolve = evolve
        self._jobs = jobs
        self._wakeup = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="skill-replay", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(5.0)

    def submit(
        self,
        skill_name: str,
        suite: EvalSuite,
        patch: SkillPatchProposal | None = None,
    ) -> None:
        payload = {"skill_name":skill_name,"suite":suite.model_dump(mode="json"),
                   "patch":patch.model_dump(mode="json") if patch else None}
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        self._jobs.enqueue(job_type="skill.evaluate",handler_version="skill-replay-v1",
                           input=payload,dedupe_key="skill.evaluate:"+digest)
        self._wakeup.set()

    def process_pending(self) -> int:
        processed = 0
        while True:
            # Replaying a crashed worker may duplicate external skill-file effects.
            # Keep an expired claim visible for explicit retry instead.
            job=self._jobs.claim(job_type="skill.evaluate",handler_version="skill-replay-v1",
                                 worker_id="skill-replay",lease_ms=300000,reclaim_expired=False)
            if job is None:
                return processed
            stopped=threading.Event()
            def heartbeat():
                while not stopped.wait(60):
                    try:
                        self._jobs.renew(job,lease_ms=300000)
                    except Exception:
                        logger.exception("skill replay lease lost")
                        return
            thread=threading.Thread(target=heartbeat,daemon=True)
            thread.start()
            try:
                data=job.input
                result=self._evolve.evaluate_and_commit(data["skill_name"],
                    EvalSuite.model_validate(data["suite"]),
                    patch=SkillPatchProposal.model_validate(data["patch"]) if data["patch"] else None)
                self._jobs.complete(job,{"admitted":result})
            except Exception as exc:
                logger.exception("skill replay failed")
                try:
                    self._jobs.fail(job,repr(exc),available_at=self._jobs.clock(),max_attempts=1)
                except Exception:
                    logger.exception("skill replay claim no longer current")
            finally:
                stopped.set()
                thread.join()
            processed+=1

    def _loop(self) -> None:
        while self._running:
            try:
                self.process_pending()
            except Exception:
                logger.exception("skill replay polling failed")
            self._wakeup.wait(0.5)
            self._wakeup.clear()


class SkillEvolution:
    def __init__(
        self,
        *,
        store,
        jobs,
        creator: SkillCreator,
        distiller: TrajectorySkillDistiller,
        optimizer: SkillOptimizer,
        gate: SkillLifecycleGate,
        repository: SkillRepository,
        llm_reflector=None,
        runner_factory: RunnerFactory | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._creator = creator
        self._distiller = distiller
        self._optimizer = optimizer
        self._gate = gate
        self._repo = repository
        self._llm_reflector = llm_reflector
        self._runner_factory = runner_factory
        self._work_dir = Path(work_dir) if work_dir is not None else (
            repository.root.parent / "skill-replay"
        )
        self.worker = SkillReplayWorker(self, jobs)

    def consider(self, turn: TurnRecord) -> None:
        session = self._store.get_session(turn.session_id)
        if session is None:
            logger.warning("skill evolution 找不到 session turn_id=%s", turn.turn_id)
            return
        facts = build_turn_facts(turn, session)
        user_text = _user_text_of(session, turn)
        proposal = self._distiller.consider(turn, facts, user_text=user_text)
        if proposal is not None:
            self._ingest_proposal(
                proposal.model_copy(
                    update={
                        "scope_key": proposal.scope_key or turn.scope.storage_key,
                        "required_tools": proposal.required_tools,
                    }
                ),
                user_text,
                scope=turn.scope,
            )
        if self._llm_reflector is not None:
            try:
                for prop in self._llm_reflector.reflect_skills(
                    turn, facts, user_text=user_text
                ):
                    self._ingest_proposal(
                        prop.model_copy(update={"scope_key": turn.scope.storage_key}),
                        user_text,
                        scope=turn.scope,
                    )
            except Exception:
                logger.exception("llm skill reflect 失败 turn_id=%s", turn.turn_id)
        created = self._creator.consider(turn, facts, scope_key=turn.scope.storage_key)
        if created is not None:
            self._enqueue_promote(created.parent.name, user_text, scope=turn.scope)
        for patch in self._optimizer.propose(turn, session, facts):
            self._apply_or_enqueue_patch(patch, user_text, scope=turn.scope)

    def evaluate_and_commit(
        self,
        skill_name: str,
        suite: EvalSuite,
        *,
        patch: SkillPatchProposal | None = None,
    ) -> bool:
        if self._runner_factory is None:
            logger.info("无 runner_factory，技能保持 draft skill=%s", skill_name)
            return False
        from ..eval.shadow import ShadowReplayRequest, run_shadow_replay

        require_improvement = patch is None
        result = run_shadow_replay(
            skills_root=self._repo.root,
            request=ShadowReplayRequest(
                skill_name=skill_name,
                suite=suite,
                promote=True,
                patch=patch,
                require_improvement=require_improvement,
            ),
            runner_factory=self._runner_factory,
            work_dir=self._work_dir / skill_name,
        )
        if patch is not None:
            if not result.gate.admitted:
                logger.info(
                    "patch 被拒绝 skill=%s reason=%s",
                    skill_name, result.gate.reason,
                )
                return False
            applied = self._repo.apply(
                SkillChangeRequest(
                    action="patch",
                    name=patch.skill_name,
                    old_string=patch.old_string,
                    new_string=patch.new_string,
                    actor="replay_gate",
                    reason=patch.rationale,
                    turn_id=patch.turn_id,
                )
            )
            return applied.ok
        promoted = self._gate.promote_after_replay(skill_name, result.gate)
        logger.info(
            "replay 结束 skill=%s admitted=%s promoted=%s reason=%s",
            skill_name, result.gate.admitted, promoted, result.gate.reason,
        )
        return promoted

    def _ingest_proposal(
        self, proposal: SkillProposal, user_text: str, *, scope: MemoryScope
    ) -> None:
        path = self._creator.create_from_proposal(proposal)
        if path is None:
            return
        self._enqueue_promote(
            proposal.name, user_text, description=proposal.description, scope=scope
        )

    def _enqueue_promote(
        self,
        skill_name: str,
        user_text: str,
        *,
        description: str = "",
        patch: SkillPatchProposal | None = None,
        scope: MemoryScope | None = None,
    ) -> None:
        if self._runner_factory is None:
            return
        try:
            suite = suite_for_skill(
                skill_name=skill_name,
                user_text=user_text,
                description=description,
                scope=scope,
            )
        except ValueError:
            logger.info("缺少回放任务，保持 draft skill=%s", skill_name)
            return
        self.worker.submit(skill_name, suite, patch)

    def _apply_or_enqueue_patch(
        self,
        patch: SkillPatchProposal,
        user_text: str,
        *,
        scope: MemoryScope,
    ) -> None:
        skill = self._repo.get(patch.skill_name)
        if skill is None:
            return
        if skill.status == "draft":
            applied = self._repo.apply(
                SkillChangeRequest(
                    action="patch",
                    name=patch.skill_name,
                    old_string=patch.old_string,
                    new_string=patch.new_string,
                    actor="skill_optimizer",
                    reason=patch.rationale,
                    turn_id=patch.turn_id,
                )
            )
            logger.info(
                "draft patch skill=%s ok=%s code=%s",
                patch.skill_name, applied.ok, applied.code,
            )
            return
        if skill.status == "active":
            self._enqueue_promote(
                patch.skill_name,
                user_text,
                description=skill.description,
                patch=patch,
                scope=scope,
            )


def _user_text_of(session, turn: TurnRecord) -> str:
    for message in reversed(getattr(session, "messages", []) or []):
        if getattr(message, "message_id", None) == turn.user_message_id:
            return getattr(message, "content_text", "") or ""
        if getattr(message, "role", None) == "user":
            return getattr(message, "content_text", "") or ""
    return ""
