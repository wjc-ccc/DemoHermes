"""Memory 抽取等固定标签任务的指标计算。"""
from __future__ import annotations

from .types import (
    ClassificationReport,
    ClassificationRequest,
    LabelMetric,
)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def run_classification(request: ClassificationRequest) -> ClassificationReport:
    samples = request.samples
    metrics: list[LabelMetric] = []
    for label in request.labels:
        true_positive = sum(
            sample.expected_label == label and sample.actual_label == label
            for sample in samples
        )
        false_positive = sum(
            sample.expected_label != label and sample.actual_label == label
            for sample in samples
        )
        false_negative = sum(
            sample.expected_label == label and sample.actual_label != label
            for sample in samples
        )
        precision = _ratio(true_positive, true_positive + false_positive)
        recall = _ratio(true_positive, true_positive + false_negative)
        f1 = _ratio(2 * precision * recall, precision + recall)
        metrics.append(
            LabelMetric(
                label=label,
                support=sum(sample.expected_label == label for sample in samples),
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )

    correct = sum(
        sample.expected_label == sample.actual_label
        for sample in samples
    )
    return ClassificationReport(
        total=len(samples),
        correct=correct,
        accuracy=_ratio(correct, len(samples)),
        macro_f1=(
            sum(metric.f1 for metric in metrics) / len(metrics)
            if metrics
            else 0.0
        ),
        labels=tuple(metrics),
        errors=tuple(
            sample
            for sample in samples
            if sample.expected_label != sample.actual_label
        ),
    )
