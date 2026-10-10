"""A completed turn yields an experience card used in the next real model call."""
from agent.types.eval.cases import EvalCase, EvalSuite, EvalTurn, TurnExpectation
from agent.eval.runner import EvalRunner
from agent.types.memory.extraction import MemoryProposal, MemoryExtractionBatch
from agent.types.model import ModelResponse


class _ScriptModel:
    name = "script"

    def __init__(self) -> None:
        self._responses = iter((
            ModelResponse(text="收到"),
            ModelResponse(text="先激活项目虚拟环境，再运行 pytest。"),
        ))

    def chat(self, messages, tools=None, **kwargs):
        return next(self._responses)


class _ExperienceExtractor:
    version = "experience-eval-v1"

    def extract_batch(self, turns, sessions):
        turn, session = turns[-1], sessions[-1]
        user = next(message for message in session.messages
                    if message.message_id == turn.user_message_id)
        if "测试通过" not in user.content_text:
            return MemoryExtractionBatch()
        return MemoryExtractionBatch(proposals=(MemoryProposal(
            proposal_id="experience-1",
            kind="reusable_experience",
            title="Windows 项目测试", summary="使用虚拟环境运行 pytest",
            content="一次观察：Windows 项目在虚拟环境运行 pytest 后测试通过。",
            keywords=("Windows", "pytest", "虚拟环境"),
            applicability="在 Windows 项目运行 pytest 时",
            action="使用项目虚拟环境运行 pytest",
            outcome="该次测试通过",
            limitations="仅在该项目观察过一次",
            outcome_status="success",
            confidence=0.8, source_authority="model_inferred",
            source_message_ids=(turn.user_message_id,),
            evidence_quotes={turn.user_message_id: user.content_text},
            reason_code="observed_test_environment",
        ),))


def test_extracted_experience_is_visible_on_next_turn(tmp_path):
    suite = EvalSuite(
        name="memory_lifecycle", version="2.0.0",
        cases=(EvalCase(
            case_id="next_turn_experience",
            turns=(
                EvalTurn(
                    user_text="Windows 项目在虚拟环境运行 pytest，测试通过了。",
                    expectation=TurnExpectation(
                        experience_lacks=("经验卡片",), reply_contains=("收到",),
                    ),
                ),
                EvalTurn(
                    user_text="Windows 项目 pytest 怎么运行？",
                    expectation=TurnExpectation(
                        experience_contains=("项目虚拟环境",),
                        reply_contains=("项目虚拟环境",),
                    ),
                ),
            ),
        ),),
    )
    report = EvalRunner(
        memory_root=tmp_path / "memory", db_path=tmp_path / "eval.db",
        model=_ScriptModel(), memory_extractor_factory=_ExperienceExtractor,
    ).run(suite)

    assert report.passed is True
    first, second = report.results[0].turns
    assert first.model_calls[0].experience_text == ""
    assert "项目虚拟环境" in second.model_calls[0].experience_text
