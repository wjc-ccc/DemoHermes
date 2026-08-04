"""HeldOutSelector — RSEA 风格 held-out 门控（8.04 Phase 5.1）。

固定小任务集：候选 insight 假想应用后，required tips 仍在、forbidden 不进新内容。
不写盘；与 EvolutionCommitGate 串联使用。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from .types import EvolutionInsight, GateDecision


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold().strip()


class HeldOutTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    required_tips: tuple[str, ...] = ()
    forbidden_in_new: tuple[str, ...] = ()


class HeldOutSelector:
    """候选 playbook 回放不降级才 admit。"""

    def __init__(self, tasks: Sequence[HeldOutTask] = ()) -> None:
        self._tasks = tuple(tasks)

    def admit(
        self,
        insight: EvolutionInsight,
        existing_contents: Sequence[str] = (),
    ) -> GateDecision:
        contents = list(existing_contents)

        if insight.op == "add":
            new_c = (insight.content or "").strip()
            for task in self._tasks:
                for bad in task.forbidden_in_new:
                    if _norm(bad) and _norm(bad) in _norm(new_c):
                        return GateDecision(
                            admitted=False, reason="held_out_forbidden",
                        )
            return GateDecision(admitted=True, reason="ok")

        if insight.op == "remove":
            target = (insight.content or "").strip()
            remaining = [
                c for c in contents
                if _norm(c) != _norm(target)
            ]
            if not self._required_still_present(remaining):
                return GateDecision(
                    admitted=False, reason="held_out_required_missing",
                )
            return GateDecision(admitted=True, reason="ok")

        if insight.op == "replace":
            new_c = (insight.content or "").strip()
            for task in self._tasks:
                for tip in task.required_tips:
                    tip_n = _norm(tip)
                    if not tip_n:
                        continue
                    if tip_n not in _norm(new_c):
                        others = [c for c in contents if tip_n in _norm(c)]
                        if len(others) <= 1:
                            return GateDecision(
                                admitted=False,
                                reason="held_out_required_missing",
                            )
                for bad in task.forbidden_in_new:
                    if _norm(bad) and _norm(bad) in _norm(new_c):
                        return GateDecision(
                            admitted=False, reason="held_out_forbidden",
                        )
            return GateDecision(admitted=True, reason="ok")

        return GateDecision(admitted=False, reason="empty_insight")

    def _required_still_present(self, contents: Sequence[str]) -> bool:
        blob = "\n".join(_norm(c) for c in contents)
        for task in self._tasks:
            for tip in task.required_tips:
                tip_n = _norm(tip)
                if tip_n and tip_n not in blob:
                    return False
        return True
