"""E1：EvalRunner.run(suite) → EvalReport（8.15）。

公共 seam：EvalRunner.run / EvalSuite / EvalReport。
Fake 仅用于 Model；经真实 MemoryManager + Gateway 观测 snapshot 与回复。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Agent.eval import (
    EvalAssertion,
    EvalCase,
    EvalRunner,
    EvalSeedMemory,
    EvalSuite,
)
from Agent.model.baseStructure import ModelResponse


class _FakeModel:
    name = "fake"

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        return self._script.pop(0)


def test_run_passes_when_snapshot_and_reply_match(tmp_path):
    """种子记忆进 snapshot；假模型回复含期望片段 → Report.passed。"""
    tip = "用户偏好：回复用简体中文"
    suite = EvalSuite(
        name="e1_smoke",
        cases=(
            EvalCase(
                name="pref_in_context",
                user_text="你好",
                seeds=(
                    EvalSeedMemory(target="memory", content=tip),
                ),
                assertions=(
                    EvalAssertion(kind="snapshot_contains", value=tip),
                    EvalAssertion(kind="reply_contains", value="你好"),
                    EvalAssertion(kind="completed", value=True),
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
    assert report.results[0].name == "pref_in_context"
    assert report.results[0].passed is True


def test_run_fails_when_snapshot_missing_expected(tmp_path):
    """断言 snapshot 含不存在片段 → 该 case 失败，Report.passed=False。"""
    suite = EvalSuite(
        name="e1_fail",
        cases=(
            EvalCase(
                name="missing_tip",
                user_text="hi",
                seeds=(),
                assertions=(
                    EvalAssertion(
                        kind="snapshot_contains",
                        value="这条策略根本不存在",
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
    assert any("snapshot_contains" in f for f in report.results[0].failures)


def test_run_aggregates_mixed_cases(tmp_path):
    """多 case：一过一不过 → 汇总正确。"""
    tip = "避坑：不要静默跳过工具失败"
    suite = EvalSuite(
        name="e1_mixed",
        cases=(
            EvalCase(
                name="ok",
                user_text="继续",
                seeds=(EvalSeedMemory(target="memory", content=tip),),
                assertions=(
                    EvalAssertion(kind="snapshot_contains", value=tip),
                    EvalAssertion(kind="completed", value=True),
                ),
            ),
            EvalCase(
                name="bad",
                user_text="继续",
                seeds=(),
                assertions=(
                    EvalAssertion(kind="reply_contains", value="绝对不会出现的字串XYZ"),
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
    by_name = {r.name: r for r in report.results}
    assert by_name["ok"].passed is True
    assert by_name["bad"].passed is False
