"""Eval 的版本化考卷、类型化期望与真实 turn 证据。"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MemorySeedTarget = Literal["policy", "project", "user"]


class MemorySeed(BaseModel):
    """Case 运行前写入隔离 Memory 的已知事实。"""

    model_config = ConfigDict(frozen=True)

    target: MemorySeedTarget
    content: str


class TurnExpectation(BaseModel):
    """对一次真实 turn 的类型化行为期望。"""

    model_config = ConfigDict(frozen=True)

    # skill_contains 断言的是 L1 摘要里出现了技能名；
    # skill_loaded 断言技能正文真的被 use_skill 加载过——只有后者能证明技能起了作用
    skill_loaded: tuple[str, ...] = ()
    skill_not_loaded: tuple[str, ...] = ()
    max_tool_failures: int | None = None
    context_contains: tuple[str, ...] = ()
    context_lacks: tuple[str, ...] = ()
    memory_contains: tuple[str, ...] = ()
    memory_lacks: tuple[str, ...] = ()
    daily_contains: tuple[str, ...] = ()
    daily_lacks: tuple[str, ...] = ()
    provider_contains: tuple[str, ...] = ()
    provider_lacks: tuple[str, ...] = ()
    skill_contains: tuple[str, ...] = ()
    skill_lacks: tuple[str, ...] = ()
    reply_contains: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()
    completed: bool | None = True
    persistence_succeeded: bool | None = True
    max_context_chars: int | None = None


class EvalTurn(BaseModel):
    """Case 中按顺序执行的一次用户 turn。"""

    model_config = ConfigDict(frozen=True)

    user_text: str
    expectation: TurnExpectation = Field(default_factory=TurnExpectation)


class EvalCase(BaseModel):
    """彼此隔离、内部可多 turn 的评测场景。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    turns: tuple[EvalTurn, ...]
    seeds: tuple[MemorySeed, ...] = ()
    tags: tuple[str, ...] = ()
    channel: str = "cli"
    user_id: str = ""

    @model_validator(mode="after")
    def _require_turn(self) -> "EvalCase":
        if not self.turns:
            raise ValueError("EvalCase.turns 不能为空")
        return self


class EvalSuite(BaseModel):
    """有版本的一组固定 EvalCase。

    target_skills 声明「这份考卷是哪些技能的考题」：候选技能据此找到自己的
    目标 case，同一份 suite 里的其余 case 天然充当回归集。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    cases: tuple[EvalCase, ...]
    target_skills: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_cases(self) -> "EvalSuite":
        if not self.cases:
            raise ValueError("EvalSuite.cases 不能为空")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("EvalSuite.case_id 必须唯一")
        return self

    def fingerprint(self) -> str:
        """考卷内容指纹；防止两份报告用同名同版本但不同期望做比较。"""
        payload = self.model_dump_json(exclude={"target_skills"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
    daily_text: str = ""
    provider_text: str = ""
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


AblationAssessment = Literal["keep", "remove", "redundant", "inconclusive"]


class AblationVariant(BaseModel):
    """关闭某层或某项资产后得到的 Eval 报告。"""

    model_config = ConfigDict(frozen=True)

    name: str
    report: EvalReport


class AblationRequest(BaseModel):
    """一次消融比较的输入。"""

    model_config = ConfigDict(frozen=True)

    baseline: EvalReport
    variants: tuple[AblationVariant, ...]


class AblationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    assessment: AblationAssessment
    pass_rate_delta: float
    context_chars_delta: int
    regressed_case_ids: tuple[str, ...] = ()
    improved_case_ids: tuple[str, ...] = ()


class AblationReport(BaseModel):
    """消融矩阵的机器可读结果与人类可读摘要。"""

    model_config = ConfigDict(frozen=True)

    suite_name: str
    suite_version: str
    results: tuple[AblationResult, ...]

    def to_markdown(self) -> str:
        labels = {
            "keep": "建议保留",
            "remove": "建议删除",
            "redundant": "证据显示冗余",
            "inconclusive": "证据不足",
        }
        lines = [
            f"# 消融评测：{self.suite_name}",
            "",
            "| 变体 | 结论 | 通过率变化 | 上下文字符变化 | 回退 Case | 改善 Case |",
            "|---|---|---:|---:|---|---|",
        ]
        for result in self.results:
            lines.append(
                "| "
                + " | ".join(
                    (
                        result.name.replace("|", "\\|"),
                        labels[result.assessment],
                        f"{result.pass_rate_delta:+.1%}",
                        f"{result.context_chars_delta:+d}",
                        ", ".join(result.regressed_case_ids) or "-",
                        ", ".join(result.improved_case_ids) or "-",
                    )
                )
                + " |"
            )
        return "\n".join(lines) + "\n"


class ClassificationSample(BaseModel):
    """分类型 Eval 的单条期望与实际结果。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    expected_label: str
    actual_label: str
    tags: tuple[str, ...] = ()


class ClassificationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    samples: tuple[ClassificationSample, ...]
    labels: tuple[str, ...]


class LabelMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    support: int
    precision: float
    recall: float
    f1: float


class ClassificationReport(BaseModel):
    """Memory 抽取等分类任务的精确率/召回率报告。"""

    model_config = ConfigDict(frozen=True)

    total: int
    correct: int
    accuracy: float
    macro_f1: float
    labels: tuple[LabelMetric, ...]
    errors: tuple[ClassificationSample, ...]

    def to_markdown(self) -> str:
        lines = [
            "# 分类评测",
            "",
            f"- 准确率：{self.accuracy:.1%}（{self.correct}/{self.total}）",
            f"- macro F1：{self.macro_f1:.1%}",
            "",
            "| Label | Support | Precision | Recall | F1 |",
            "|---|---:|---:|---:|---:|",
        ]
        for metric in self.labels:
            lines.append(
                f"| {metric.label} | {metric.support} | "
                f"{metric.precision:.1%} | {metric.recall:.1%} | {metric.f1:.1%} |"
            )
        return "\n".join(lines) + "\n"
