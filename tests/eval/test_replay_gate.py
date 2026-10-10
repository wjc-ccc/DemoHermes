"""Replay Gate：逐 case 单调性比总通过率更严格。"""
from agent.types.eval.reports import EvalCaseResult, EvalReport
from agent.eval.replay import compare_reports


def _report(name: str, outcomes: dict[str, bool], *, version: str = "1") -> EvalReport:
    results = tuple(
        EvalCaseResult(case_id=case_id, passed=passed)
        for case_id, passed in outcomes.items()
    )
    passed_count = sum(outcomes.values())
    return EvalReport(
        suite_name=name,
        suite_version=version,
        passed=passed_count == len(outcomes),
        passed_count=passed_count,
        total_count=len(outcomes),
        results=results,
    )


def test_replay_rejects_any_regression_even_when_total_score_is_unchanged():
    baseline = _report("held_out", {"stable": True, "target": False})
    candidate = _report("held_out", {"stable": False, "target": True})

    result = compare_reports(baseline, candidate)

    assert result.admitted is False
    assert result.reason == "regression"
    assert result.regressed_case_ids == ("stable",)
    assert result.improved_case_ids == ("target",)


def test_generated_asset_requires_at_least_one_improved_case():
    baseline = _report("held_out", {"stable": True, "still_bad": False})
    candidate = _report("held_out", {"stable": True, "still_bad": False})

    result = compare_reports(baseline, candidate, require_improvement=True)

    assert result.admitted is False
    assert result.reason == "no_improvement"


def test_neutral_playbook_change_can_pass_no_regression_policy():
    baseline = _report("held_out", {"stable": True})
    candidate = _report("held_out", {"stable": True})

    result = compare_reports(baseline, candidate)

    assert result.admitted is True
    assert result.reason == "no_regression"


def test_replay_rejects_behavior_regression_even_when_case_still_passes():
    from agent.types.eval.reports import EvalTurnResult, ToolCallEvidence

    def _with_failures(case_id: str, failures: int) -> EvalCaseResult:
        tools = tuple(
            ToolCallEvidence(name="calculator", ok=False, error="boom")
            for _ in range(failures)
        )
        turn = EvalTurnResult(
            turn_index=0,
            passed=True,
            tool_calls=tools,
        )
        return EvalCaseResult(case_id=case_id, passed=True, turns=(turn,))

    baseline = EvalReport(
        suite_name="held_out",
        suite_version="1",
        passed=True,
        passed_count=1,
        total_count=1,
        results=(_with_failures("stable", 0),),
    )
    candidate = EvalReport(
        suite_name="held_out",
        suite_version="1",
        passed=True,
        passed_count=1,
        total_count=1,
        results=(_with_failures("stable", 2),),
    )
    result = compare_reports(baseline, candidate)
    assert result.admitted is False
    assert result.reason == "behavior_regression"
    assert result.behavior_regressed_case_ids == ("stable",)


def test_replay_rejects_different_suite_versions():
    baseline = _report("held_out", {"stable": True}, version="1")
    candidate = _report("held_out", {"stable": True}, version="2")

    result = compare_reports(baseline, candidate)

    assert result.admitted is False
    assert result.reason == "suite_mismatch"
