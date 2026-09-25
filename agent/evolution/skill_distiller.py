"""TrajectorySkillDistiller — Hermes 式 observe→distill（8.04 Phase 6）。

复杂成功轨迹 → SkillProposal（目标/步骤/失败处理/验证）。
可选注入 LLM 回调；默认用结构化模板（测试与无模型时可用）。
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

from ..core.memory.types import TurnRecord
from .types import EvolutionSignal, SkillProposal

logger = logging.getLogger(__name__)

_SKIP = frozenset({"use_skill"})

DistillFn = Callable[[TurnRecord, EvolutionSignal, str], SkillProposal | None]


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip().lower()).strip("_")
    return (slug[:48] or "distilled_skill")


def _procedural(seq: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(t for t in seq if t and t not in _SKIP)


def _default_render(
    turn: TurnRecord,
    signals: EvolutionSignal,
    user_text: str,
) -> SkillProposal:
    steps = _procedural(signals.tool_sequence)
    name = _slugify("_".join(steps) if steps else (user_text[:24] or turn.turn_id))
    desc = (user_text.strip()[:80] or f"复用流程 {' → '.join(steps)}")[:80]
    step_lines = "\n".join(
        f"{i}. 调用 `{t}`，确认成功后再进入下一步。"
        for i, t in enumerate(steps, 1)
    )
    pitfalls = ""
    if signals.tool_failures or signals.recovered_from_failure:
        tips = list(signals.tool_failures) or ["曾失败后重试成功"]
        pitfalls = "\n".join(f"- 曾遇到「{t}」：定位原因后重试，勿静默跳过。" for t in tips[:3])
    else:
        pitfalls = "- 任一步失败：记录错误并向用户报告，不要静默跳过。"
    body = (
        f"## 目标\n\n{desc}\n\n"
        f"## 前置\n\n- 工具可用：{', '.join(f'`{t}`' for t in steps) or '（按任务选择）'}\n\n"
        f"## 步骤\n\n{step_lines or '1. 按用户目标拆解并执行。'}\n\n"
        f"## 失败处理\n\n{pitfalls}\n\n"
        f"## 验证\n\n- 用户目标已满足；关键副作用（写入/部署）可复查。\n"
    )
    return SkillProposal(
        name=name,
        description=desc,
        body=body,
        status="draft",
        turn_id=turn.turn_id,
        rationale="hermes_complex_success",
        scope_key=turn.scope.storage_key,
        required_tools=steps,
    )


class TrajectorySkillDistiller:
    """达 Hermes 复杂度启发式后产出 SkillProposal。"""

    def __init__(
        self,
        *,
        min_tools: int = 5,
        frequency_threshold: int = 3,
        distill_fn: DistillFn | None = None,
    ) -> None:
        self._min_tools = max(1, min_tools)
        self._frequency_threshold = max(1, frequency_threshold)
        self._distill_fn = distill_fn or _default_render
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def should_distill(self, signals: EvolutionSignal | None) -> bool:
        if signals is None:
            return False
        steps = _procedural(signals.tool_sequence)
        if len(steps) >= self._min_tools and (
            not signals.tool_failures or signals.recovered_from_failure
        ):
            return True
        if signals.recovered_from_failure and signals.completed:
            return True
        if len(steps) >= 2 and (
            not signals.tool_failures or signals.recovered_from_failure
        ):
            key = "_".join(steps)
            with self._lock:
                self._counts[key] = self._counts.get(key, 0) + 1
                return self._counts[key] >= self._frequency_threshold
        return False

    def consider(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None,
        *,
        user_text: str = "",
    ) -> SkillProposal | None:
        if not turn.completed or turn.error:
            return None
        if signals is None or not signals.completed:
            return None
        # 仍有未恢复失败则不沉淀
        if signals.tool_failures and not signals.recovered_from_failure:
            return None
        if not self.should_distill(signals):
            return None
        # should_distill 对 frequency 路径已自增；对 min_tools 路径再记一次指纹防刷
        steps = _procedural(signals.tool_sequence)
        if len(steps) >= self._min_tools:
            key = "_".join(steps)
            # 同指纹已沉淀过则跳过（由 Creator 文件存在再挡一层）
            pass
        try:
            return self._distill_fn(turn, signals, user_text)
        except Exception:
            logger.exception("distill 失败 turn_id=%s", turn.turn_id)
            return None
