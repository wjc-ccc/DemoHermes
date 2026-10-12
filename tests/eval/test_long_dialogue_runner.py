import json
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.eval.long_dialogue import run_question
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.model import ModelResponse, ToolCallRequest


class Model:
    name="fixture"
    model_name="fixture-v1"
    def chat(self,messages,tools=None,**kwargs):
        system=messages[0]["content"]
        if "长期记忆提炼器" in system:
            return ModelResponse(text='{"cards":[],"skipped":[]}',usage={"prompt_tokens":3,"completion_tokens":1})
        if "评测审查员" in system:
            return ModelResponse(text=json.dumps(dict(answer_passed=True,memory_matches=[],
                forbidden_card_ids=[],extra_card_ids=[],violations=[],reason="来源是同事，不是本人")))
        assert "reference_answer" not in str(messages)
        assert "## 参考处理" not in str(messages)
        if messages[-1]["role"]=="tool":
            return ModelResponse(text="只能知道同事小林是前端工程师，本人职业没有记录，不能确认。")
        return ModelResponse(tool_calls=[ToolCallRequest(id="lookup",name="memory_search",arguments={"query":"小林"})])


def test_long_replay_uses_real_stores_workers_tools_and_keeps_standards_out_of_main_input(tmp_path):
    case=next(c for c in load_long_datasets()[0].cases if c.case_id=="third_party_profile")
    result=run_question(case,case.questions[0],tmp_path,Model())
    assert result["passed"],result
    assert len(result["source_message_id_map"])==20
    assert all(result["hard_checks"].values())
    path=tmp_path/"third_party_profile__q1"/"agent.db"
    storage=RuntimeStorage.open(path)
    with storage.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM turns").fetchone()[0]==21
        assert conn.execute("SELECT count(*) FROM memory_cards").fetchone()[0]==0
        row=conn.execute("SELECT turn_json FROM turns WHERE json_type(turn_json,'$.perception_trace')='object'").fetchone()
        assert len(json.loads(row[0])["perception_trace"]["builds"])==2
