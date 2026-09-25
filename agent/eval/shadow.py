"""Shadow replay：在隔离技能目录里假想晋升/打补丁，再和 baseline 比较。"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..evolution.types import ReplayGateResult, SkillPatchProposal
from ..skills import SkillRegistry, SkillRepository
from ..skills.repository import SkillChangeRequest
from .replay import compare_reports
from .types import EvalReport, EvalSuite

RunnerFactory = Callable[[SkillRegistry], "EvalRunnerLike"]


class EvalRunnerLike:
    def run(self, suite: EvalSuite) -> EvalReport: ...


class ShadowReplayRequest(BaseModel):
    """一次影子回放：对照当前磁盘，在副本上应用候选变更。"""

    model_config = ConfigDict(frozen=True)

    skill_name: str
    suite: EvalSuite
    promote: bool = True
    patch: SkillPatchProposal | None = None
    require_improvement: bool = True


class ShadowReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate: ReplayGateResult
    baseline: EvalReport
    candidate: EvalReport


def run_shadow_replay(
    *,
    skills_root: Path,
    request: ShadowReplayRequest,
    runner_factory: RunnerFactory,
    work_dir: Path,
) -> ShadowReplayResult:
    """复制技能目录 → 在副本上晋升/打补丁 → 跑同一份 suite → 逐 case 比较。

    生产目录只在调用方根据 gate.admitted 决定是否提交；本函数绝不写回生产。
    """
    skills_root = Path(skills_root)
    work_dir = Path(work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    if skills_root.exists():
        shutil.copytree(skills_root, work_dir)
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    baseline_registry = SkillRegistry(skill_dirs=[skills_root])
    baseline = runner_factory(baseline_registry).run(request.suite)

    shadow_registry = SkillRegistry(skill_dirs=[work_dir])
    shadow_repo = SkillRepository(work_dir, registry=shadow_registry)
    if request.patch is not None:
        patched = shadow_repo.apply(
            SkillChangeRequest(
                action="patch",
                name=request.patch.skill_name,
                old_string=request.patch.old_string,
                new_string=request.patch.new_string,
                actor="shadow_replay",
                reason=request.patch.rationale,
                turn_id=request.patch.turn_id,
            )
        )
        if not patched.ok:
            empty = _empty_report(request.suite)
            return ShadowReplayResult(
                gate=ReplayGateResult(
                    admitted=False,
                    reason=f"shadow_patch_failed:{patched.code}",
                ),
                baseline=baseline,
                candidate=empty,
            )
    if request.promote:
        promoted = shadow_repo.apply(
            SkillChangeRequest(
                action="promote",
                name=request.skill_name,
                actor="shadow_replay",
                reason="candidate isolation",
            )
        )
        if not promoted.ok and promoted.code != "already_active":
            empty = _empty_report(request.suite)
            return ShadowReplayResult(
                gate=ReplayGateResult(
                    admitted=False,
                    reason=f"shadow_promote_failed:{promoted.code}",
                ),
                baseline=baseline,
                candidate=empty,
            )

    candidate = runner_factory(shadow_registry).run(request.suite)
    return ShadowReplayResult(
        gate=compare_reports(
            baseline,
            candidate,
            require_improvement=request.require_improvement,
        ),
        baseline=baseline,
        candidate=candidate,
    )


def _empty_report(suite: EvalSuite) -> EvalReport:
    return EvalReport(
        suite_name=suite.name,
        suite_version=suite.version,
        passed=False,
        passed_count=0,
        total_count=len(suite.cases),
        suite_fingerprint=suite.fingerprint(),
    )
