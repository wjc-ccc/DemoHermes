"""Memory 生命周期：Reviewer 写入只影响下一 turn 的实际 memory 分层。"""
from agent.eval import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    EvalTurn,
    TurnExpectation,
)
from agent.core.memory import RuleBasedMemoryExtractor
from agent.model.baseStructure import ModelResponse


class _ScriptModel:
    name = "script"

    def __init__(self) -> None:
        self._responses = iter(
            (
                ModelResponse(text="收到"),
                ModelResponse(text="我会先给结论"),
            )
        )

    def chat(self, messages, tools=None, **kwargs):
        return next(self._responses)


def test_reviewer_memory_stays_structured_and_does_not_mutate_user_book(tmp_path):
    preference = "请记住我偏好先给结论"
    suite = EvalSuite(
        name="memory_lifecycle",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="next_turn_visibility",
                turns=(
                    EvalTurn(
                        user_text=preference,
                        expectation=TurnExpectation(
                            memory_lacks=(preference,),
                            reply_contains=("收到",),
                        ),
                    ),
                    EvalTurn(
                        user_text="继续回答",
                        expectation=TurnExpectation(
                            memory_lacks=(preference,),
                            reply_contains=("先给结论",),
                        ),
                    ),
                ),
            ),
        ),
    )
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_ScriptModel(),
        memory_extractor_factory=RuleBasedMemoryExtractor,
    )

    report = runner.run(suite)

    assert report.passed is True
    first, second = report.results[0].turns
    assert preference not in first.model_calls[0].memory_text
    assert preference not in second.model_calls[0].memory_text
