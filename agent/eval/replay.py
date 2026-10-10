"""EvalReport 的逐 case 单调性比较，供候选资产提交门使用。"""
from __future__ import annotations

from agent.types.eval.reports import ReplayGateResult
from agent.types.eval.reports import EvalReport


def compare_reports(
    baseline: EvalReport,
    candidate: EvalReport,
    *,
    require_improvement: bool = False,
) -> ReplayGateResult:
    """同一份考卷的 candidate 不得使任何已通过 case 回退。

    除了通过/失败翻转，还拒绝「仍然通过但工具失败变多」的行为回退。
    考卷内容指纹或模型环境指纹不同时直接拒绝比较。
    """
    if (
        baseline.suite_name != candidate.suite_name
        or baseline.suite_version != candidate.suite_version
    ):
        return ReplayGateResult(admitted=False, reason="suite_mismatch")
    if (
        baseline.suite_fingerprint
        and candidate.suite_fingerprint
        and baseline.suite_fingerprint != candidate.suite_fingerprint
    ):
        return ReplayGateResult(admitted=False, reason="suite_mismatch")
    if (
        baseline.environment_fingerprint
        and candidate.environment_fingerprint
        and baseline.environment_fingerprint != candidate.environment_fingerprint
    ):
        return ReplayGateResult(admitted=False, reason="environment_mismatch")

    baseline_cases = {result.case_id: result for result in baseline.results}
    candidate_cases = {result.case_id: result for result in candidate.results}
    if baseline_cases.keys() != candidate_cases.keys():
        return ReplayGateResult(admitted=False, reason="case_set_mismatch")

    regressed: list[str] = []
    improved: list[str] = []
    unchanged: list[str] = []
    behavior_regressed: list[str] = []
    for case_id in sorted(baseline_cases):
        before = baseline_cases[case_id]
        after = candidate_cases[case_id]
        if before.passed and not after.passed:
            regressed.append(case_id)
        elif not before.passed and after.passed:
            improved.append(case_id)
        else:
            unchanged.append(case_id)
            if after.passed and after.tool_failure_count > before.tool_failure_count:
                behavior_regressed.append(case_id)

    if regressed:
        admitted, reason = False, "regression"
    elif behavior_regressed:
        admitted, reason = False, "behavior_regression"
    elif require_improvement and not improved:
        admitted, reason = False, "no_improvement"
    else:
        admitted = True
        reason = "improved" if improved else "no_regression"
    return ReplayGateResult(
        admitted=admitted,
        reason=reason,
        regressed_case_ids=tuple(regressed),
        improved_case_ids=tuple(improved),
        unchanged_case_ids=tuple(unchanged),
        behavior_regressed_case_ids=tuple(behavior_regressed),
        context_chars_delta=(
            candidate.total_context_chars - baseline.total_context_chars
        ),
    )
