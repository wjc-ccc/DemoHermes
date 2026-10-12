"""核心评测设施：协议、隔离、真实调用记录、续跑与统计口径。"""
import json
import pytest
from pathlib import Path
from agent.eval.core_benchmark import BenchmarkModel, SourceReplayItem, execute_answer, protocol_pairs, run_case, run_reuse_case, reuse_definitions, grade_results, atomic_json
from agent.eval.core_benchmark_report import usage_summary, clustered_interval, compare
from agent.eval.datasets.core_benchmark import load_definitions
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.eval.core_benchmark import BenchmarkConfig
from agent.types.memory.scope import MemoryScope
from agent.types.model import ModelResponse


class Model:
    name="fixture"
    model_name="fixture"
    def __init__(self):self.count=0
    def chat(self,messages,tools=None,**kwargs):
        self.count+=1;system=messages[0]["content"]
        if "长期记忆提炼器" in system:text='{"cards":[],"skipped":[]}'
        elif "评测审查员" in system:text=json.dumps(dict(answer_passed=True,memory_matches=[],classification_matches=[],extra_card_ids=[],forbidden_card_ids=[],violations=[],reason="评测假模型"))
        elif "AnchorBuilder" in system:text=json.dumps(dict(intent=[],changes=[],decisions=[],next_steps=[],excluded=[]))
        else:
            assert "## 参考处理" not in str(messages)
            text="本人信息未提供、实验未执行，需要询问。"
        return ModelResponse(text=text,usage={"prompt_tokens":11,"completion_tokens":3})


def test_protocol_pairs_accepts_reused_provider_id_only_after_previous_pair_completed():
    pair=[{"role":"assistant","tool_calls":[{"id":"same"}]},{"role":"tool","tool_call_id":"same"}]
    assert protocol_pairs(pair+pair)
    assert not protocol_pairs(pair[:1]+[{"role":"user","content":"break"}])
    assert not protocol_pairs([pair[1]])


def test_recording_resumes_with_stage_and_never_zero_fills_missing_usage(tmp_path):
    path=tmp_path/"calls.jsonl";model=BenchmarkModel(Model(),path,stage="extraction",tag="M1")
    model.chat([{"role":"system","content":"独立准入复核"}])
    assert BenchmarkModel(Model(),path).calls[0]["purpose"]=="memory_admission"
    assert json.loads(path.read_text(encoding="utf-8"))["tag"]=="M1"
    value=usage_summary([{"usage":{"prompt_tokens":5,"completion_tokens":2}},{"error":"timeout"}])
    assert value["input_tokens"] is None and value["known_input_tokens"]==5
    assert value["missing_input_usage"]==1


def test_atomic_checkpoint_retries_windows_transient_replace_denial(tmp_path,monkeypatch):
    original=Path.replace;attempts=[]
    def replace(source,target):
        attempts.append(1)
        if len(attempts)==1:raise PermissionError("模拟临时文件扫描锁")
        return original(source,target)
    monkeypatch.setattr(Path,"replace",replace)
    atomic_json(tmp_path/"checkpoint.json",{"seen":3})
    assert len(attempts)==2 and json.loads((tmp_path/"checkpoint.json").read_text())=={"seen":3}


def test_source_resume_does_not_duplicate_or_read_future_and_memory_off_keeps_raw_retrieval(tmp_path):
    case=load_definitions(pilot=True)[0].cases[0];question=case.questions[0]
    store=RuntimeStorage.open(tmp_path/"source.db");scope=MemoryScope(tenant_id="test",user_id="u")
    model=BenchmarkModel(Model(),tmp_path/"calls.jsonl")
    replay=SourceReplayItem(store,scope,model,memory=False,directory=tmp_path)
    current,previous=replay.advance(case,question)
    assert len(replay.source_map)==10
    (tmp_path/"source-state.json").unlink()
    resumed=SourceReplayItem(store,scope,model,memory=False,directory=tmp_path)
    resumed.advance(case,question)
    with store.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM turns").fetchone()[0]==10
        assert conn.execute("SELECT count(*) FROM memory_cards").fetchone()[0]==0
    answer,_,_,retriever=execute_answer(store,current,previous,scope,case,question,tmp_path/"answer",Model(),BenchmarkConfig(),perception=False,memory=False)
    assert answer["completed"] and all(answer["hard_checks"].values())
    assert retriever.read(scope,"message:"+replay.source_map[case.history[0].turns[1].turn_id])
    resumed.advance(case,case.questions[-1])
    with pytest.raises(ValueError,match="future_turns"):resumed.advance(case,question)


def test_four_conditions_save_before_blind_grading_and_resume_without_new_answers(tmp_path):
    definition=load_definitions(pilot=True)[1];case=next(case for case in definition.cases if case.scenario=="negative")
    model=Model();config=BenchmarkConfig(repeats=1,workers=1)
    run_case(definition,case,tmp_path,model,config,1)
    paths=list(tmp_path.glob("cases/**/result.json"));assert len(paths)==12
    assert all(json.loads(path.read_text(encoding="utf-8"))["grade_status"]=="pending" for path in paths)
    count=model.count
    run_case(definition,case,tmp_path,model,config,1)
    assert model.count==count
    grade_results(tmp_path,(definition,),model,config)
    assert all(json.loads(path.read_text(encoding="utf-8"))["grade_status"]=="completed" for path in paths)
    count=model.count;grade_results(tmp_path,(definition,),model,config);assert model.count==count


def test_clustered_ci_and_noninferiority_keep_failed_pairs():
    config={"random_seed":1,"bootstrap_samples":500,"noninferiority_margin":.02}
    assert clustered_interval({"a":[0,0],"b":[0]},samples=100)==[0,0]
    rows=[]
    for i in range(3):
        for perception in (0,1):rows.append(dict(case_id=str(i),question_id="q1",repeat=1,condition=f"P{perception}M0",completed=True,review={"answer_passed":not perception,"violations":[]},high_noise=True,length=40,model_calls=[dict(purpose="main",usage={"input_tokens":10 if perception else 100,"output_tokens":1})]))
    result=compare(rows,0,config)
    assert result["quality_difference"]==-1 and result["noninferiority"]=="不满足非劣"
    assert result["high_noise"]["main_input_saving"]==.9


def test_six_question_reuse_keeps_session_state_and_counts_background_calls(tmp_path):
    definition=reuse_definitions(load_definitions())[0]
    case=next(case for case in definition.cases if case.scenario=="distributed")
    run_reuse_case(definition,case,tmp_path,Model(),BenchmarkConfig(repeats=1,workers=1),1)
    initial_sources=[]
    for name in ("P0M1","P1M1"):
        directory=tmp_path/"reuse"/case.case_id/"repeat-1"/name
        sequence=json.loads((directory/"sequence.json").read_text(encoding="utf-8"));rows=sequence["results"]
        assert [row["warm_step"] for row in rows]==[1,2,3,4,5,6]
        session_ids={row["source"]["session_id"] for row in rows};assert len(session_ids)==1
        store=RuntimeStorage.open(directory/"agent.db")
        with store.db.read() as conn:
            assert conn.execute("SELECT count(*) FROM turns").fetchone()[0]==sum(len(history.turns) for history in case.history)+6
        assert sum(len(row["source"]["source_calls"]) for row in rows)==len(sequence["source_calls"])
        initial_sources.append(rows[0]["source"]["source_map"])
    assert initial_sources[0]==initial_sources[1]
    count=Model();run_reuse_case(definition,case,tmp_path,count,BenchmarkConfig(repeats=1,workers=1),1)
    assert count.count==0
