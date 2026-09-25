"""
turn_facts — 从已落库事实构造 EvolutionSignal

为什么不用 EventBus：Loop 只是把事件投进异步队列，Gateway 在 commit 成功后立刻
执行进化 hook，两者之间没有 barrier。此时读 EvolutionSignalCollector 可能拿到 None
或缺少 turn_end / 后续失败的半截信号，于是「是否沉淀、是否晋升」变成时序赌博。

沉淀与晋升是有副作用的决定，必须基于确定性事实：已提交的 TurnRecord + Session 里
持久化的 ToolCall。EventBus 保留给前端观测，丢事件不再影响进化正确性。
"""
from __future__ import annotations

from ..core.types import Session, ToolCall
from ..core.memory.types import TurnRecord
from .types import EvolutionSignal

_SKILL_TOOL = "use_skill"


def turn_tool_calls(turn: TurnRecord, session: Session) -> list[ToolCall]:
    """本 turn 的工具调用，按发生顺序。"""
    calls = [
        call
        for call in (session.tool_calls or [])
        if call.trajectory_id == turn.turn_id
    ]
    calls.sort(key=lambda call: call.created_at)
    return calls


def build_turn_facts(turn: TurnRecord, session: Session) -> EvolutionSignal:
    """已提交 turn → 完整、可复现的 EvolutionSignal。"""
    calls = turn_tool_calls(turn, session)
    failures: list[str] = []
    skills_used: list[str] = []
    recovered = False
    failed_tools: set[str] = set()

    for call in calls:
        name = (call.tool_name or "").strip()
        if not call.ok:
            failures.append(f"{name or 'tool'}: {call.error or 'failed'}")
            if name:
                failed_tools.add(name)
            continue
        # 同一个工具先失败、后成功才算「问题被解决」；
        # 换条路绕过去不是 recovered，不应据此沉淀经验
        if name in failed_tools:
            recovered = True
        if name == _SKILL_TOOL:
            skill = _skill_name_of(call)
            if skill and skill not in skills_used:
                skills_used.append(skill)

    return EvolutionSignal(
        turn_id=turn.turn_id,
        completed=turn.completed and not turn.interrupted,
        interrupted=turn.interrupted,
        error=turn.error,
        iteration_count=len(turn.assistant_message_ids),
        tool_failures=tuple(failures),
        tool_sequence=tuple((call.tool_name or "").strip() for call in calls),
        skills_used=tuple(skills_used),
        recovered_from_failure=recovered,
    )


def skill_failure_attribution(
    turn: TurnRecord,
    session: Session,
) -> dict[str, tuple[str, ...]]:
    """把工具失败归因到「当时正在生效的技能」。

    一次 use_skill 之后、下一次 use_skill 之前的失败，才算这个技能的失败；
    否则一条报错会被写进本轮用到的所有技能，污染无关资产。
    """
    attribution: dict[str, list[str]] = {}
    current: str | None = None
    for call in turn_tool_calls(turn, session):
        name = (call.tool_name or "").strip()
        if name == _SKILL_TOOL:
            current = _skill_name_of(call) if call.ok else None
            if current:
                attribution.setdefault(current, [])
            continue
        if current and not call.ok:
            attribution.setdefault(current, []).append(
                f"{name or 'tool'}: {call.error or 'failed'}"
            )
    return {
        skill: tuple(failures)
        for skill, failures in attribution.items()
        if failures
    }


def _skill_name_of(call: ToolCall) -> str:
    data = call.tool_call_result or {}
    return str(
        data.get("skill_name") or (call.arguments or {}).get("skill_name") or ""
    ).strip()
