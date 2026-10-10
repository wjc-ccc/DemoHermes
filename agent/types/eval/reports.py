"""eval / reports 数据契约。"""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field


class ToolCallEvidence(BaseModel):
    """一次真实工具调用的事实；use_skill 的技能名与版本也记录在这里。"""

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    skill_name: str = ""
    skill_revision: int = 0
    error: str = ""


class ModelCallEvidence(BaseModel):
    """模型 adapter 实际收到的一次请求证据。"""

    model_config = ConfigDict(frozen=True)

    call_index: int
    context_text: str
    context_chars: int
    memory_text: str = ""
    experience_text: str = ""
    skill_text: str = ""


class EvalFailure(BaseModel):
    """稳定失败码与便于排查的细节。"""

    model_config = ConfigDict(frozen=True)

    code: str
    detail: str


class EvalTurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_index: int
    passed: bool
    failures: tuple[EvalFailure, ...] = ()
    reply_text: str = ""
    completed: bool = False
    persistence_succeeded: bool = False
    model_calls: tuple[ModelCallEvidence, ...] = ()
    tool_calls: tuple[ToolCallEvidence, ...] = ()

    @property
    def tool_failure_count(self) -> int:
        return sum(1 for call in self.tool_calls if not call.ok)


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    passed: bool
    turns: tuple[EvalTurnResult, ...] = ()

    @property
    def tool_failure_count(self) -> int:
        return sum(turn.tool_failure_count for turn in self.turns)

    @property
    def context_chars(self) -> int:
        return sum(
            call.context_chars for turn in self.turns for call in turn.model_calls
        )


class EvalReport(BaseModel):
    """run(suite) 的汇总结果。"""

    model_config = ConfigDict(frozen=True)

    suite_name: str
    suite_version: str
    passed: bool
    passed_count: int
    total_count: int
    results: tuple[EvalCaseResult, ...] = Field(default_factory=tuple)
    # 考卷内容指纹与运行环境；两者不同的报告不允许直接比较
    suite_fingerprint: str = ""
    environment_fingerprint: str = ""

    @property
    def pass_rate(self) -> float:
        return self.passed_count / self.total_count if self.total_count else 0.0

    @property
    def total_context_chars(self) -> int:
        return sum(
            call.context_chars
            for case in self.results
            for turn in case.turns
            for call in turn.model_calls
        )

    def to_markdown(self) -> str:
        """生成适合 CI artifact / 人工复核的小型可视化摘要。"""
        lines = [
            f"# Eval：{self.suite_name}",
            "",
            f"- 版本：`{self.suite_version}`",
            f"- 通过率：{self.pass_rate:.1%}（{self.passed_count}/{self.total_count}）",
            "",
            "| Case | 状态 | Turns | 模型调用 | 最大上下文字符 | 失败码 |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for case in self.results:
            calls = [call for turn in case.turns for call in turn.model_calls]
            max_chars = max((call.context_chars for call in calls), default=0)
            failure_codes = sorted(
                {
                    failure.code
                    for turn in case.turns
                    for failure in turn.failures
                }
            )
            lines.append(
                "| "
                + " | ".join(
                    (
                        case.case_id.replace("|", "\\|"),
                        "PASS" if case.passed else "FAIL",
                        str(len(case.turns)),
                        str(len(calls)),
                        str(max_chars),
                        ", ".join(failure_codes) or "-",
                    )
                )
                + " |"
            )
        return "\n".join(lines) + "\n"


class ReplayGateResult(BaseModel):
    """baseline/candidate 逐 case 比较后的质量门结果。"""

    model_config = ConfigDict(frozen=True)

    admitted: bool
    reason: str
    regressed_case_ids: tuple[str, ...] = ()
    improved_case_ids: tuple[str, ...] = ()
    unchanged_case_ids: tuple[str, ...] = ()
    # 行为回退：case 仍然通过，但新增了工具失败
    behavior_regressed_case_ids: tuple[str, ...] = ()
    # 成本变化：候选相对 baseline 的上下文字符增量
    context_chars_delta: int = 0
