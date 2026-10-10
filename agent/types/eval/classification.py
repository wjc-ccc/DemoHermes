"""eval / classification 数据契约。"""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict


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
