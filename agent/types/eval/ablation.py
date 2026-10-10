"""eval / ablation 数据契约。"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict

from agent.types.eval.reports import EvalReport


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
