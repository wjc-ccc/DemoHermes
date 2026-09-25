"""Eval 公共 seam：类型化 case 经真实 Gateway turn 产出实际模型上下文证据。"""
from __future__ import annotations

from agent.eval import (
    EvalCase,
    EvalRunner,
    EvalTurn,
    EvalSuite,
    MemorySeed,
    TurnExpectation,
)
from agent.model.baseStructure import ModelResponse


class _FakeModel:
    name = "fake"

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        return self._script.pop(0)


def test_run_uses_the_context_received_by_the_model_as_evidence(tmp_path):
    """种子记忆必须进入真实模型请求，而不是 Runner 另算的探针快照。"""
    tip = "用户偏好：回复用简体中文"
    suite = EvalSuite(
        name="e1_smoke",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="pref_in_context",
                seeds=(
                    MemorySeed(target="user", content=tip),
                ),
                turns=(
                    EvalTurn(
                        user_text="你好",
                        expectation=TurnExpectation(
                            memory_contains=(tip,),
                            reply_contains=("你好",),
                            completed=True,
                            persistence_succeeded=True,
                        ),
                    ),
                ),
            ),
        ),
    )
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel([ModelResponse(text="你好，有什么可以帮忙？")]),
    )
    report = runner.run(suite)

    assert report.suite_name == "e1_smoke"
    assert report.passed is True
    assert report.passed_count == 1
    assert report.total_count == 1
    assert report.results[0].case_id == "pref_in_context"
    assert report.results[0].passed is True
    turn = report.results[0].turns[0]
    assert tip in turn.model_calls[0].context_text
    assert turn.persistence_succeeded is True
    markdown = report.to_markdown()
    assert "通过率：100.0%" in markdown
    assert "pref_in_context" in markdown
    assert "上下文字符" in markdown


def test_run_fails_when_actual_model_context_misses_expected_content(tmp_path):
    suite = EvalSuite(
        name="e1_fail",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="missing_tip",
                turns=(
                    EvalTurn(
                        user_text="hi",
                        expectation=TurnExpectation(
                            memory_contains=("这条策略根本不存在",),
                        ),
                    ),
                ),
            ),
        ),
    )
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel([ModelResponse(text="hi")]),
    )
    report = runner.run(suite)

    assert report.passed is False
    assert report.passed_count == 0
    assert report.total_count == 1
    assert report.results[0].passed is False
    assert "memory.missing" in {
        failure.code for failure in report.results[0].turns[0].failures
    }


def test_run_aggregates_mixed_cases(tmp_path):
    """多 case：一过一不过 → 汇总正确。"""
    tip = "避坑：不要静默跳过工具失败"
    suite = EvalSuite(
        name="e1_mixed",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="ok",
                seeds=(MemorySeed(target="user", content=tip),),
                turns=(
                    EvalTurn(
                        user_text="继续",
                        expectation=TurnExpectation(
                            memory_contains=(tip,),
                            completed=True,
                        ),
                    ),
                ),
            ),
            EvalCase(
                case_id="bad",
                turns=(
                    EvalTurn(
                        user_text="继续",
                        expectation=TurnExpectation(
                            reply_contains=("绝对不会出现的字串XYZ",),
                        ),
                    ),
                ),
            ),
        ),
    )
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel(
            [
                ModelResponse(text="好的"),
                ModelResponse(text="好的"),
            ]
        ),
    )
    report = runner.run(suite)

    assert report.passed is False
    assert report.passed_count == 1
    assert report.total_count == 2
    by_id = {r.case_id: r for r in report.results}
    assert by_id["ok"].passed is True
    assert by_id["bad"].passed is False


def test_runner_can_be_reused_without_leaking_case_memory(tmp_path):
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel(
            [
                ModelResponse(text="first"),
                ModelResponse(text="second"),
            ]
        ),
    )
    first = EvalSuite(
        name="repeatable",
        version="1",
        cases=(
            EvalCase(
                case_id="same",
                seeds=(MemorySeed(target="user", content="仅第一次存在"),),
                turns=(EvalTurn(user_text="run one"),),
            ),
        ),
    )
    second = EvalSuite(
        name="repeatable",
        version="1",
        cases=(
            EvalCase(
                case_id="same",
                turns=(
                    EvalTurn(
                        user_text="run two",
                        expectation=TurnExpectation(
                            context_lacks=("仅第一次存在",),
                        ),
                    ),
                ),
            ),
        ),
    )

    assert runner.run(first).passed is True
    report = runner.run(second)

    assert report.passed is True
    assert "仅第一次存在" not in report.results[0].turns[0].model_calls[0].context_text
