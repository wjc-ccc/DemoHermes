"""Memory Layer 与 Perception 优先级正交，三本常驻文档作为冻结快照进入上下文。"""
from agent.types.eval.cases import EvalCase, EvalSuite, EvalTurn, MemorySeed
from agent.eval.runner import EvalRunner
from agent.types.model import ModelResponse


class _FakeModel:
    name = "fake"

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text="ok")


def test_policy_project_and_user_books_share_one_frozen_snapshot(tmp_path):
    suite = EvalSuite(
        name="layered_books",
        version="1",
        cases=(
            EvalCase(
                case_id="all_layers",
                seeds=(
                    MemorySeed(target="policy", content="生产数据库不可直接写"),
                    MemorySeed(target="project", content="测试命令是 pytest"),
                    MemorySeed(target="user", content="用户偏好中文回答"),
                ),
                turns=(EvalTurn(user_text="继续"),),
            ),
        ),
    )

    report = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel(),
    ).run(suite)

    call = report.results[0].turns[0].model_calls[0]
    assert "生产数据库不可直接写" in call.memory_text
    assert "测试命令是 pytest" in call.memory_text
    assert "用户偏好中文回答" in call.memory_text
    assert call.experience_text == ""
