"""评测通过生产装配运行；离线测试仅替换外部模型边界。"""
from agent import main
from agent.model.registry import ModelRegistry
from types import SimpleNamespace
import pytest


def test_eval_backend_keeps_database_and_manuals_in_selected_directory(tmp_path, monkeypatch):
    registry = ModelRegistry()
    registry.register(SimpleNamespace(name="offline"))
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    gateway = main.build_gateway(data_root=tmp_path)
    try:
        assert gateway.observe_databases["agent"] == tmp_path / "agent.db"
        assert (tmp_path / "agent.db").exists()
        assert (tmp_path / "skills").is_dir()
        assert (tmp_path / "memory").is_dir()
    finally:
        gateway.stop()


@pytest.mark.parametrize("cards,expected,error", [
    ([], [{"kind": "profile", "anchors": [["Python"]]}], "missing_expected_card"),
    ([{"item_id": "a", "kind": "profile", "content": "Python"}], [], "unexpected_card"),
    ([{"item_id": "a", "kind": "ongoing_fact", "content": "Python"}],
     [{"kind": "profile", "anchors": [["Python"]]}], "missing_expected_card"),
    ([{"item_id": "a", "kind": "profile", "content": "Python Redis"}],
     [{"kind": "profile", "anchors": [["Python"]]},
      {"kind": "profile", "anchors": [["Redis"]]}], "missing_expected_card"),
])
def test_eval_does_not_count_missing_extra_wrong_kind_or_merged_cards_as_pass(cards, expected, error):
    from agent.eval.memory_behavior import score_cards
    score = score_cards(expected, cards)
    assert not score["passed"]
    assert error in {issue["code"] for issue in score["issues"]}


def test_http_dialogue_background_card_and_evidence_can_be_read_in_new_session(tmp_path, monkeypatch):
    import json
    from agent.model.baseStructure import ModelResponse, ToolCallRequest
    from agent.eval.memory_behavior import run_suite

    class ExternalModelFixture:
        name = "offline_fixture"

        def chat(self, messages, tools=None, **kwargs):
            if "长期记忆提炼器" not in messages[0]["content"]:
                if messages[-1]["role"] == "tool":
                    result = json.loads(messages[-1]["content"])
                    if "hits" in result:
                        return ModelResponse(text="", tool_calls=[ToolCallRequest(
                            id="read-profile", name="memory_read",
                            arguments={"reference": result["hits"][0]["reference"]},
                        )])
                    return ModelResponse(text="你是 Python 后端工程师。")
                if "检索职业" in messages[-1]["content"]:
                    return ModelResponse(text="", tool_calls=[ToolCallRequest(
                        id="search-profile", name="memory_search",
                        arguments={"query": "Python", "source": "card"},
                    )])
                return ModelResponse(text="收到。")
            source = json.loads(messages[-1]["content"])["sources"][0]
            if not source["user_text"].startswith("我是"):
                return ModelResponse(text="[]")
            return ModelResponse(text=json.dumps([{
                "kind": "profile", "subject": "user", "attribute": "profession",
                "summary": "Python 后端工程师", "content": "用户是 Python 后端工程师。",
                "keywords": ["Python", "后端", "工程师"], "confidence": 0.9,
                "source_message_ids": [source["user_message_id"]],
                "evidence_quotes": {source["user_message_id"]: source["user_text"]},
                "reason_code": "stable_profession",
            }], ensure_ascii=False))

    registry = ModelRegistry()
    registry.register(ExternalModelFixture())
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    suite = {"name": "offline_boundary_check", "version": "1", "cases": [{
        "id": "profile", "description": "稳定职业", "turns": ["我是 Python 后端工程师。"],
        "expected": [{"kind": "profile", "anchors": [["Python"], ["后端"]], "query": "Python"}],
        "probe": {"text": "请检索职业", "anchors": [["Python"]], "tools": ["memory_search", "memory_read"]},
    }]}
    report = run_suite(suite, tmp_path / "run", wait_seconds=5)
    assert report["passed"], report
    case = report["cases"][0]
    assert case["database"]["jobs"][0]["status"] == "completed"
    assert case["checks"]["exact_evidence"]
    assert case["checks"]["scope_isolation"]
    assert case["retrieval"][0]["found_expected_card"]
    assert case["probe"]["checks"]["read_expected_card"]
    assert (tmp_path / "run" / "report.json").exists()


def test_multi_session_case_extracts_after_each_session(tmp_path, monkeypatch):
    from agent.model.baseStructure import ModelResponse
    from agent.eval.memory_behavior import run_suite

    class EmptyExternalModel:
        name = "empty_fixture"

        def chat(self, messages, tools=None, **kwargs):
            return ModelResponse(text="[]" if "长期记忆提炼器" in messages[0]["content"] else "好的。")

    registry = ModelRegistry()
    registry.register(EmptyExternalModel())
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    suite = {"name": "multi_session", "version": "1", "cases": [{
        "id": "two", "description": "两个会话", "sessions": [["你好"], ["谢谢", "再见"]], "expected": [],
    }]}
    report = run_suite(suite, tmp_path / "run", wait_seconds=5)
    case = report["cases"][0]
    assert report["passed"], case
    assert case["input"] == ["你好", "谢谢", "再见"]
    assert len({turn["session_id"] for turn in case["database"]["turns"]}) == 2
    assert all(job["status"] == "completed" for job in case["database"]["jobs"])


def test_malformed_model_proposal_is_not_a_successful_empty_extraction(tmp_path, monkeypatch):
    from agent.model.baseStructure import ModelResponse
    from agent.eval.memory_behavior import run_suite

    class InvalidExternalModel:
        name = "invalid_fixture"

        def chat(self, messages, tools=None, **kwargs):
            if "长期记忆提炼器" in messages[0]["content"]:
                # 真实模型曾输出这两种错误字段类型，不能把拒绝它们算作“选择不记”。
                return ModelResponse(text='[{"kind":"profile","confidence":"high","evidence_quotes":[]}]')
            return ModelResponse(text="你好。")

    registry = ModelRegistry()
    registry.register(InvalidExternalModel())
    monkeypatch.setattr(ModelRegistry, "default", classmethod(lambda cls: registry))
    suite = {"name": "invalid_is_not_empty", "version": "1", "cases": [{
        "id": "greeting", "description": "寒暄", "turns": ["你好"], "expected": [],
    }]}
    report = run_suite(suite, tmp_path / "run", wait_seconds=5)
    case = report["cases"][0]
    assert case["cards"] == []
    assert case["database"]["jobs"][0]["status"] == "completed"
    assert not case["checks"]["no_parse_issues"]
    assert not report["passed"]
