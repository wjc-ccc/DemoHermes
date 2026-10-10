"""Memory/Skill 消融：关闭资产后以任务回退和成本变化判断贡献。"""
from agent.types.eval.ablation import AblationRequest, AblationVariant
from agent.types.eval.reports import EvalCaseResult, EvalReport
from agent.eval.ablation import run_ablation


def _report(outcomes: dict[str, bool]) -> EvalReport:
    results = tuple(
        EvalCaseResult(case_id=case_id, passed=passed)
        for case_id, passed in outcomes.items()
    )
    passed_count = sum(outcomes.values())
    return EvalReport(
        suite_name="ablation",
        suite_version="1",
        passed=passed_count == len(outcomes),
        passed_count=passed_count,
        total_count=len(outcomes),
        results=results,
    )


def test_ablation_recommends_keep_when_disabling_asset_regresses_cases():
    report = run_ablation(
        AblationRequest(
            baseline=_report({"memory_recall": True, "plain_chat": True}),
            variants=(
                AblationVariant(
                    name="without_hot_memory",
                    report=_report({"memory_recall": False, "plain_chat": True}),
                ),
            ),
        )
    )

    result = report.results[0]
    assert result.assessment == "keep"
    assert result.regressed_case_ids == ("memory_recall",)
    assert result.pass_rate_delta == -0.5
    markdown = report.to_markdown()
    assert "without_hot_memory" in markdown
    assert "建议保留" in markdown


def test_ablation_marks_asset_redundant_when_disabling_changes_no_case():
    report = run_ablation(
        AblationRequest(
            baseline=_report({"plain_chat": True}),
            variants=(
                AblationVariant(
                    name="without_unused_skill",
                    report=_report({"plain_chat": True}),
                ),
            ),
        )
    )

    assert report.results[0].assessment == "redundant"
