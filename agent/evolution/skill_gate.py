"""SkillLifecycleGate — draft → active 只经回放质量门。

生产路径禁止「用了两次就晋升」。隔离副本上的假想晋升走 SkillRepository，
正式晋升必须拿到 admitted 且至少改善一个 case 的 ReplayGateResult。
"""
from __future__ import annotations

import logging

from ..skills.repository import SkillChangeRequest, SkillRepository
from .types import ReplayGateResult

logger = logging.getLogger(__name__)


class SkillLifecycleGate:
    """Skill 生命周期门：晋升 / 停用都委托 SkillRepository。"""

    def __init__(
        self,
        registry,
        *,
        repository: SkillRepository | None = None,
        success_threshold: int = 2,
    ) -> None:
        # success_threshold 仅保留兼容旧构造；不再用于晋升。
        if success_threshold < 1:
            raise ValueError("success_threshold 必须 >= 1")
        self._registry = registry
        self._repo = repository

    def _repository(self) -> SkillRepository:
        if self._repo is not None:
            return self._repo
        skill = next(iter(self._registry._skills.values()), None)
        if skill is None or not skill.path:
            raise RuntimeError("SkillLifecycleGate 需要 SkillRepository 或已扫描的技能路径")
        from pathlib import Path

        root = Path(skill.path).parent.parent
        self._repo = SkillRepository(root, registry=self._registry)
        return self._repo

    def record_success(self, skill_name: str) -> bool:
        """计数晋升已关闭：再成功多少次也不会把 draft 变成 active。"""
        del skill_name
        return False

    def promote_for_replay(self, skill_name: str) -> bool:
        """仅用于隔离副本：将 draft 假想为 active 后执行 candidate replay。"""
        return self._apply("promote", skill_name, actor="shadow_replay")

    def promote_after_replay(
        self,
        skill_name: str,
        result: ReplayGateResult,
    ) -> bool:
        """生成型 Skill 只有零回退且至少改善一个 case 才能正式晋升。"""
        if not result.admitted or not result.improved_case_ids:
            return False
        if result.behavior_regressed_case_ids:
            return False
        return self._apply("promote", skill_name, actor="replay_gate")

    def disable(self, skill_name: str, *, reason: str = "") -> bool:
        return self._apply("disable", skill_name, actor="replay_gate", reason=reason)

    def rollback(self, skill_name: str, *, target_revision: int | None = None) -> bool:
        repo = self._repository()
        applied = repo.apply(
            SkillChangeRequest(
                action="rollback",
                name=skill_name,
                target_revision=target_revision,
                actor="replay_gate",
                reason="rollback",
            )
        )
        return applied.ok

    def _apply(
        self,
        action: str,
        skill_name: str,
        *,
        actor: str,
        reason: str = "",
    ) -> bool:
        name = (skill_name or "").strip()
        if not name:
            return False
        applied = self._repository().apply(
            SkillChangeRequest(action=action, name=name, actor=actor, reason=reason)
        )
        if applied.ok:
            logger.info("Skill %s name=%s", action, name)
        return applied.ok
