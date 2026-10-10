"""同一 suite 的资产关闭变体比较。"""
from __future__ import annotations

from .replay import compare_reports
from agent.types.eval.ablation import AblationReport, AblationRequest, AblationResult


def run_ablation(request: AblationRequest) -> AblationReport:
    """比较 baseline 与各 disabled variant，给出资产贡献判断。"""
    baseline = request.baseline
    results: list[AblationResult] = []
    for variant in request.variants:
        comparison = compare_reports(baseline, variant.report)
        regressed = comparison.regressed_case_ids
        improved = comparison.improved_case_ids
        if comparison.reason in {"suite_mismatch", "case_set_mismatch"}:
            assessment = "inconclusive"
        elif regressed and improved:
            assessment = "inconclusive"
        elif regressed:
            assessment = "keep"
        elif improved:
            assessment = "remove"
        else:
            assessment = "redundant"
        results.append(
            AblationResult(
                name=variant.name,
                assessment=assessment,
                pass_rate_delta=variant.report.pass_rate - baseline.pass_rate,
                context_chars_delta=(
                    variant.report.total_context_chars
                    - baseline.total_context_chars
                ),
                regressed_case_ids=regressed,
                improved_case_ids=improved,
            )
        )
    return AblationReport(
        suite_name=baseline.suite_name,
        suite_version=baseline.suite_version,
        results=tuple(results),
    )
