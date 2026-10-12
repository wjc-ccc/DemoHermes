"""冻结模块的长对话四组评测：真实调用、隔离数据库、续跑及连续复用。"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import random
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from agent.eval.datasets.core_benchmark import load_definitions
from agent.eval.long_dialogue import RecordedModel, REVIEW_PROMPT, _validate_review
from agent.core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.building_messages import build_assistant_message, build_tool_message
from agent.core.event_bus import EventBus
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.core.perception.token_estimator import SafeEstimator
from agent.core.tool_dispatch import ToolDispatch
from agent.core.tool_handler import ToolHandler
from agent.model.call_options import structured_call_options
from agent.model.registry import ModelRegistry
from agent.prompt.renderers import render_card, source_block
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.tools.memory_tools import MemorySearchTool, MemoryReadTool
from agent.tools.memory_edit_tool import MemoryEditTool
from agent.tools.registry import ToolRegistry
from agent.types.context import ContextBudget
from agent.types.conversation import Session, Message, ToolCall
from agent.types.eval.datasets import EvalDataset
from agent.types.eval.core_benchmark import BenchmarkConfig, CONDITIONS
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.memory.categories import UserCardStatus
from agent.types.memory.items import UserMemoryItem
from agent.types.model import ToolCallRequest
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionConfig, PerceptionResult, PerceptionMetrics, PerceptionRecord
from agent.types.perception.trace import normalized_usage
from agent.types.perception.trace import response_metadata
from agent.types.runtime import LoopConfig
from agent.types.tools import ToolInvocationContext

ROOT=Path(__file__).resolve().parents[2]
RUNNER_VERSION="core-benchmark-v3"


def atomic_json(path, value):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt==19:raise
            time.sleep(.05)


def production_fingerprints():
    folders=("agent/core","agent/prompt","agent/model","agent/storage","agent/tools","agent/gateway","agent/channel","agent/skills")
    paths=[ROOT/"agent/runtime.py",ROOT/"agent/main.py",ROOT/"agent/provider.py"]
    for folder in folders:
        paths.extend(path for path in (ROOT/folder).rglob("*") if path.suffix in {".py",".sql",".md"})
    paths.extend(path for path in (ROOT/"agent/types").rglob("*.py") if "eval" not in path.relative_to(ROOT/"agent/types").parts)
    return {str(path.relative_to(ROOT)).replace("\\","/"):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}


def evaluation_fingerprint():
    digest=hashlib.sha256()
    for folder in (ROOT/"agent/eval",ROOT/"agent/types/eval"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(str(path.relative_to(ROOT)).encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def check_run(root):
    root=Path(root)
    if (root/"stop-requested").exists():raise InterruptedError("评测已请求停止，当前调用记录保留")
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    if production_fingerprints()!=manifest["production"] or evaluation_fingerprint()!=manifest["evaluation_hash"]:raise ValueError("代码指纹变化，必须新建运行版本")


class BenchmarkModel:
    """给每次真实调用增加阶段与条件标记，不修改模型输入。"""
    def __init__(self, delegate, path, *, stage="answer", tag=""):
        self.recorded=RecordedModel(delegate,path)
        self.delegate=delegate
        self.stage,self.tag=stage,tag
        self.name=self.recorded.name
        self.model_name=self.recorded.model_name
        self.supports_images=self.recorded.supports_images
        if Path(path).exists():
            self.recorded.calls=[json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

    @property
    def calls(self):
        return self.recorded.calls

    def chat(self,messages,tools=None,**kwargs):
        system=str(messages[0].get("content",""))
        purpose="review" if self.stage=="review" else "anchor" if "AnchorBuilder" in system else "memory_repair" if "有限格式/来源修复" in system else "memory_admission" if "独立准入复核" in system else "memory" if self.stage=="extraction" else "main"
        record=dict(stage=self.stage,tag=self.tag,purpose=purpose,messages=copy.deepcopy(messages),tools=copy.deepcopy(tools),parameters=kwargs,provider=self.name,model=self.model_name,started_at=time.time())
        record["call_id"]=uuid.uuid4().hex
        inflight=self.recorded.path.parent/"inflight";inflight.mkdir(exist_ok=True)
        pending=inflight/f"{record['call_id']}.json"
        atomic_json(pending,record)
        try:
            response=self.delegate.chat(messages,tools=tools,**kwargs)
            record.update(response=getattr(response,"text",None),usage=getattr(response,"usage",{}),tool_calls=[call.model_dump(mode="json") for call in getattr(response,"tool_calls",())],**response_metadata(response))
            return response
        except Exception as exc:
            record["error"]=type(exc).__name__
            record["http_status"]=getattr(getattr(exc,"response",None),"status_code",None)
            raise
        finally:
            record["duration_ms"]=int((time.time()-record["started_at"])*1000)
            self.calls.append(record)
            with self.recorded.path.open("a",encoding="utf-8") as handle:handle.write(json.dumps(record,ensure_ascii=False,default=str)+"\n")
            pending.unlink(missing_ok=True)
            print(f"[model] {self.tag} {purpose} {record['duration_ms']}ms",flush=True)


class FullInput(PerceptionBuilder):
    """关闭perception时发送同一允许原文前缀，不调用分级或Anchor。"""
    def __init__(self,*args,previous=(),**kwargs):
        super().__init__(*args,**kwargs)
        self.previous=previous

    def build(self,session,**kwargs):
        view=session.model_copy(deep=True)
        view.messages=copy.deepcopy([message for old in self.previous for message in old.messages]+session.messages)
        view.tool_calls=copy.deepcopy([tool for old in self.previous for tool in old.tool_calls]+session.tool_calls)
        for i,message in enumerate(view.messages):message.seq_id=i
        view.context_anchor,view.context_verdicts=None,{}
        loaded=self._load(view,memory_snapshot=kwargs.get("memory_snapshot"),skill_selection=kwargs.get("skill_selection"),ephemeral_prompt=kwargs.get("ephemeral_prompt"))
        messages=self._build_messages(loaded.items,())
        estimator=SafeEstimator()
        cost=estimator.estimate_messages(messages)+estimator.estimate_tools(kwargs.get("tool_schemas") or ())
        budget=self.config.budget
        if cost>budget.max_input_tokens:raise ValueError("full_input_budget_exceeded")
        records=tuple(PerceptionRecord(item_name=item.name,priority=item.priority,token_estimate=estimator.estimate_messages([item.message]),decision="selected",reason="无perception完整原文对照",kind=item.kind,role=item.message["role"],origin_ref=item.origin_ref,seq=item.seq) for item in loaded.items)
        result=PerceptionResult(messages=messages,records=records,metrics=PerceptionMetrics(trace_id=kwargs["trace_id"],budget=budget.max_input_tokens,tokens_used=cost,budget_usage=cost/budget.max_input_tokens,message_count=len(messages),total_chars=sum(len(str(message.get("content",""))) for message in messages)),output_token_limit=budget.reserved_output_tokens)
        self._trace.record(result,iteration=kwargs.get("iteration"))
        return result


class SourceReplayItem:
    """同一原始时间线只提交一次，检查点前flush并冻结，不插入答案。"""
    def __init__(self,storage,scope,model,*,memory,directory):
        self.storage,self.scope,self.memory,self.directory=storage,scope,memory,directory
        self.run_root=next((parent for parent in (directory,*directory.parents) if (parent/"manifest.json").exists()),None)
        self.source_map,self.tool_map,self.seen={}, {}, set()
        self.source_issues=[]
        self.mapping_path=directory/"source-state.json"
        if self.mapping_path.exists():
            state=json.loads(self.mapping_path.read_text(encoding="utf-8"))
            self.source_map,self.tool_map=state["source_map"],state["tool_map"]
            self.source_issues=state.get("source_issues",[])
            self.seen=set(self.source_map)
        with storage.db.read() as conn:identities=[row[0] for row in conn.execute("SELECT session_id FROM sessions")]
        for identity in identities:
                saved=storage.sessions.get_session(identity)
                for message in saved.messages:
                    identity=message.metadata.get("eval_source_turn_id")
                    if identity:self.source_map[identity]=message.message_id
                for tool in saved.tool_calls:
                    identity=tool.metadata.get("eval_source_tool_id")
                    if identity:self.tool_map[identity]=tool.tool_call_id
        self.seen=set(self.source_map)
        self.worker=MemoryReviewWorker(storage.sessions,MemoryBatchReviewer(MemoryExtractor(model,storage.memory)),storage.review_committer,extractor_version=MemoryExtractor.version,backoff_seconds=0,lease_seconds=120)

    def advance(self,case,question):
        if self.run_root:check_run(self.run_root)
        allowed={turn.turn_id for history in case.history_for(question) for turn in history.turns}
        if self.seen-allowed:raise ValueError("source_checkpoint_contains_future_turns")
        sessions=[]
        for history in case.history_for(question):
            session=self.storage.sessions.get_or_create(history.session_id,scope=self.scope,channel="benchmark",author_id=self.scope.user_id)
            sessions.append(session)
            for ordinal,declared in enumerate(history.turns):
                if self.run_root and (self.run_root/"stop-requested").exists():raise InterruptedError("评测在来源边界停止")
                if declared.turn_id in self.seen:continue
                stamp=history.occurred_at.timestamp()+ordinal*60
                turn_id=uuid.uuid4().hex
                user=session.add_message(Message(role="user",content_text=declared.user_text,created_at=stamp,trajectory_id=turn_id,metadata={"eval_source_turn_id":declared.turn_id}))
                self.source_map[declared.turn_id]=user.message_id
                assistants,tools=[],[]
                for observation in declared.tool_results:
                    args=dict(observation.arguments)
                    pending=None
                    result_text=observation.result_text
                    if observation.name=="memory_edit" and args.get("operation")=="card_delete":
                        query=str(args.pop("query",""))
                        cards=self.storage.memory.search(self.scope,query,kind="ongoing_fact",limit=100,as_of_ms=int(stamp*1000)) if self.memory else ()
                        cards=[card for card in cards if "采购" in card.content or "预算" in card.content]
                        budget_sources={self.source_map[turn.turn_id] for segment in case.history_for(question) for turn in segment.turns if turn.turn_id in self.source_map and f"{query}的采购预算" in turn.user_text}
                        exact=[card for card in cards if budget_sources&({card.source_ref}|{ref.source_id for ref in card.evidence})]
                        if exact:cards=exact
                        if len(cards)>1:
                            self.source_issues.append(dict(code="duplicate_delete_targets",turn_id=declared.turn_id,card_ids=[card.item_id for card in cards]))
                            # 历史命令只删一张目标卡；剩余重复卡交给真实违规评测。
                            cards=sorted(cards,key=lambda card:card.item_id)[:1]
                        if cards:
                            args.update(item_id=cards[0].item_id,expected_revision=cards[0].revision)
                            result=MemoryEditTool(self.storage.memory,MarkdownDocuments(self.directory/"memory",self.storage.db)).execute(args,context=ToolInvocationContext(session_id=session.session_id,turn_id=turn_id,source_message_id=user.message_id,memory_scope=self.scope))
                            if not result.ok:raise ValueError(result.error)
                            pending=result.pending_mutation
                        else:
                            result_text="当前没有可删除的长期卡片；用户遗忘要求仍保留为对话约束。"
                    request=ToolCallRequest(id=observation.provider_tool_call_id or observation.tool_call_id,name=observation.name,arguments=args,arguments_raw=json.dumps(args,ensure_ascii=False))
                    assistant=session.add_message(build_assistant_message("",[request]));assistant.created_at=stamp+.1;assistant.trajectory_id=turn_id
                    assistants.append(assistant.message_id)
                    fact_id=uuid.uuid4().hex
                    call=ToolCall(tool_call_id=fact_id,provider_tool_call_id=request.id,session_id=session.session_id,trajectory_id=turn_id,tool_name=observation.name,arguments=args,arguments_raw=request.arguments_raw,status=observation.status,result_text=result_text,result_data=observation.result_data,error=observation.error,created_at=stamp+.2,finished_at=stamp+.3,duration_ms=100,pending_mutation=pending)
                    call.metadata["eval_source_tool_id"]=observation.tool_call_id
                    session.tool_calls.append(call);tools.append(fact_id);self.tool_map[observation.tool_call_id]=fact_id
                    message=session.add_message(build_tool_message(call));message.created_at=stamp+.3;message.trajectory_id=turn_id
                assistant=session.add_message(Message(role="assistant",content_text=declared.assistant_text or "收到。",created_at=stamp+.4,trajectory_id=turn_id));assistants.append(assistant.message_id)
                self.storage.sessions.commit_turn(session,TurnRecord(turn_id=turn_id,session_id=session.session_id,scope=self.scope,user_message_id=user.message_id,assistant_message_ids=tuple(assistants),tool_call_ids=tuple(tools),started_at=stamp,finished_at=stamp+.5,completed=True,interrupted=False,error=None),scope=self.scope)
                self.seen.add(declared.turn_id)
                atomic_json(self.mapping_path,dict(source_map=self.source_map,tool_map=self.tool_map,source_issues=self.source_issues))
                if self.memory:self.worker.process_pending()
        if self.memory:
            with self.storage.db.write() as conn:self.storage.review_scheduler.flush_scope(conn,self.scope.storage_key,trigger_type="benchmark_checkpoint")
            self.worker.process_pending()
        return sessions[-1],tuple(sessions[:-1])


def components(storage,directory,scope,model,question,memory,session_id="benchmark"):
    clock=lambda:int(question.asked_at.timestamp()*1000)
    documents=MarkdownDocuments(directory/"memory",storage.db)
    retriever=MemoryRetriever(storage.db,documents=documents,now_ms=clock)
    registry=ToolRegistry()
    for tool in (MemorySearchTool(retriever),MemoryReadTool(retriever),MemoryEditTool(storage.memory,documents)):registry.register(tool)
    context=MemoryContextLoader(directory/"memory",documents=documents,document_registry=storage.documents,experience_retriever=retriever,clock=lambda:question.asked_at.timestamp())
    return retriever,registry,context.freeze(scope,session_id=session_id,query=question.text) if memory else None


def execute_answer(storage,session,previous,scope,case,question,directory,delegate,config,*,perception,memory,engine=None,model=None):
    directory.mkdir(parents=True,exist_ok=True)
    model=model or BenchmarkModel(delegate,directory/"model-calls.jsonl",tag=f"P{int(perception)}M{int(memory)}")
    retriever,registry,snapshot=components(storage,directory,scope,model,question,memory,session.session_id)
    trace=PerceptionTrace();observer=AgentLoopTrace(EventBus(),trace)
    budget=ContextBudget(model_context_tokens=config.perception_context_tokens if perception else config.full_context_tokens,reserved_output_tokens=config.output_tokens)
    if engine is None:
        options=dict(trace=trace,anchor_builder=ModelAnchorBuilder(model),admission_policy=AdmissionPolicy(mode="off"),config=PerceptionConfig(budget=budget))
        engine=PerceptionBuilder(**options) if perception else FullInput(**options,previous=previous)
    else:
        # 连续问题保留Session中的真实Anchor；Trace按问题单独归档。
        engine._trace=trace
    handler=ToolHandler(ToolDispatch(observer,registry=registry),registry=registry)
    loop=AgentLoop(model,perception=engine,tool_handler=handler,trace=observer,config=LoopConfig(max_iterations=config.max_iterations,model_retries=config.model_retries))
    originals=[message.model_dump(mode="json") for message in session.messages]
    user=Message(role="user",content_text=f"当前对话时间：{question.asked_at.isoformat()}\n{question.text}",created_at=question.asked_at.timestamp())
    begin=time.time();call_begin=len(model.calls)
    result=loop.run_turn(session,user,memory_scope=scope,memory_snapshot=snapshot)
    # 对话使用题目逻辑时钟；API日志仍保留实际墙钟与耗时。
    elapsed=time.time()-begin
    for message in session.messages[len(originals):]:
        if message.message_id!=user.message_id:message.created_at=user.created_at+max(0,message.created_at-begin)
    for tool in session.tool_calls:
        if tool.trajectory_id==result.turn_id:
            tool.created_at=user.created_at+max(0,tool.created_at-begin)
            tool.finished_at=user.created_at+max(0,tool.finished_at-begin)
    observed=trace.get(result.turn_id)
    turn=TurnRecord(turn_id=result.turn_id,session_id=session.session_id,scope=scope,user_message_id=user.message_id,assistant_message_ids=tuple(message.message_id for message in session.messages if message.role=="assistant" and message.seq_id>user.seq_id),tool_call_ids=tuple(tool.tool_call_id for tool in session.tool_calls if tool.trajectory_id==result.turn_id),started_at=user.created_at,finished_at=user.created_at+elapsed,completed=result.completed,interrupted=result.interrupted,error=result.error,perception_trace=observed.model_dump(mode="json") if observed else None,input_token_count=observed.input_token_count if observed else None,output_token_count=observed.output_token_count if observed else None)
    persistence_error=None
    try:storage.sessions.commit_turn(session,turn,scope=scope)
    except Exception as exc:persistence_error=f"{type(exc).__name__}: {exc}"
    trace.finish(result.turn_id)
    with storage.db.read() as conn:
        calls=[json.loads(row[0]) for row in conn.execute("SELECT tool_call_json FROM tool_calls WHERE trajectory_id=?",(result.turn_id,))] if "trajectory_id" in {row[1] for row in conn.execute("pragma table_info(tool_calls)")} else [tool.model_dump(mode="json") for tool in session.tool_calls if tool.trajectory_id==result.turn_id]
        fk=conn.execute("pragma foreign_key_check").fetchall()
    builds=observed.builds if observed else ()
    hard=dict(raw_unchanged=originals==[message.model_dump(mode="json") for message in session.messages[:len(originals)]],foreign_keys_ok=not fk,budget_ok=all(build.metrics is None or build.metrics.budget_usage<=1 for build in builds),protected_context_ok=all(build.metrics is None or (build.metrics.p0_dropped_count==0 and build.metrics.p1_dropped_count==0 and build.metrics.protected_error_missing_count==0) for build in builds))
    hard["protocol_pairs_ok"]=all(protocol_pairs(build.messages) for build in builds if build.messages is not None)
    hard["output_budget_ok"]=all(call.get("parameters",{}).get("max_tokens",config.output_tokens)<=config.output_tokens for call in model.calls[call_begin:] if call["purpose"]=="main")
    hard["iteration_limit_ok"]=len(builds)<=config.max_iterations
    with storage.db.read() as conn:hard["scope_isolated"]=all(row[0]==scope.storage_key for row in conn.execute("SELECT scope_key FROM sessions"))
    retrieval=[hit.reference.removeprefix("card:") for hit in retriever.search(scope,question.text,source="card",limit=10)] if memory else []
    return dict(actual_answer=result.final_text or "",completed=result.completed and not persistence_error,error=persistence_error or result.error,duration_ms=int((time.time()-begin)*1000),input_tokens=observed.input_token_count if observed else None,output_tokens=observed.output_token_count if observed else None,trace=observed.model_dump(mode="json") if observed else None,tool_calls=calls,model_calls=copy.deepcopy(model.calls[call_begin:]),hard_checks=hard,retrieval_ids=retrieval),engine,model,retriever


def protocol_pairs(messages):
    pending=[]
    for message in messages:
        if message.get("role")=="tool":
            identity=message.get("tool_call_id")
            if identity not in pending:return False
            pending.remove(identity)
        else:
            if pending:return False
            pending=[call["id"] for call in message.get("tool_calls",())]
    return not pending


def evidence_sources(sources,observed_tools):
    """只沿成功读回的真实结构字段追踪来源，不从引用正文猜测ID。"""
    result=set(sources)
    for call in observed_tools:
        if call.get("status")!="success":continue
        data=call.get("result_data") or {};context=data.get("context") or {}
        refs=[call.get("readback_reference"),data.get("reference"),context.get("source_ref")]
        refs.extend(context.get("evidence_refs") or ())
        for hit in data.get("hits") or ():
            refs.append(hit.get("reference"));refs.extend(hit.get("evidence_refs") or ())
        if any(isinstance(ref,str) and ref.split(":",1)[-1] in sources for ref in refs):result.add(call["tool_call_id"])
    return result


def candidates(case,question,cards,source_map,tool_map,observed_tools=()):
    selected=set(question.expected_active_memory_ids or ())
    turns={turn.turn_id:turn for history in case.history for turn in history.turns}
    result={}
    for expected in case.expected_memories:
        if expected.memory_id not in selected:continue
        sources={source_map[identity] for identity in expected.evidence_turn_ids if identity in source_map}
        sources.update(tool_map[tool.tool_call_id] for identity in expected.evidence_turn_ids for tool in turns[identity].tool_results if tool.tool_call_id in tool_map)
        sources=evidence_sources(sources,observed_tools)
        result[expected.memory_id]=[card.item_id for card in cards if card.kind==expected.kind and (expected.valid_until is None or card.valid_until==expected.valid_until) and (expected.outcome_status is None or card.outcome_status==expected.outcome_status) and sources&({card.source_ref}|{ref.source_id for ref in card.evidence})]
    return result


def all_cards(storage,scope,question):
    cards=[]
    while True:
        page=storage.memory.search(scope,"",limit=100,offset=len(cards),as_of_ms=int(question.asked_at.timestamp()*1000))
        cards.extend(page)
        if len(page)<100:return tuple(cards)


def finish_background(storage,scope,out,delegate):
    """等价于回答后的空闲提炼；产品后台调用独立记录。"""
    model=BenchmarkModel(delegate,out/"answer-memory-calls.jsonl",stage="extraction",tag="answer-background")
    with storage.db.write() as conn:storage.review_scheduler.flush_scope(conn,scope.storage_key,trigger_type="benchmark_answer_idle")
    worker=MemoryReviewWorker(storage.sessions,MemoryBatchReviewer(MemoryExtractor(model,storage.memory)),storage.review_committer,extractor_version=MemoryExtractor.version,backoff_seconds=0,lease_seconds=120)
    worker.process_pending()
    return copy.deepcopy(model.calls)


def blind_review(delegate,path,case,question,answer,cards,source_map,tool_map,*,review_index=0,observed_tools=()):
    model=BenchmarkModel(delegate,path,stage="review",tag="blinded")
    allowed=candidates(case,question,cards,source_map,tool_map,observed_tools)
    lines=["## 当前问题",question.text,"## 参考处理",question.reference_answer,"## 必需含义",*question.required_meaning,"## 禁止含义",*question.forbidden_meaning,"## 实际回答",answer,"## 预期有效记忆"]
    lines.extend(f"{item.memory_id}: {item.kind.value}；{item.claim}；条件：{'、'.join(item.conditions)}" for item in case.expected_memories if item.memory_id in allowed)
    lines.append("## 实际卡片")
    lines.extend(render_card(card) for card in cards)
    lines.append("## 候选对应关系")
    lines.extend(f"预期 {identity} -> 实际候选：{'、'.join(values) or '无'}" for identity,values in allowed.items())
    lines.append("## 允许原文（不包含未来）")
    for history in case.history_for(question):
        for turn in history.turns:
            lines.append(source_block(source_map[turn.turn_id],"user",turn.user_text))
            if turn.turn_id in question.evidence_turn_ids:
                lines.extend(source_block(tool_map.get(tool.tool_call_id,tool.tool_call_id),"tool",tool.result_text or tool.error or "",name=tool.name,status=tool.status) for tool in turn.tool_results)
    broad={}
    turns={turn.turn_id:turn for history in case.history for turn in history.turns}
    for expected in case.expected_memories:
        if expected.memory_id not in allowed:continue
        sources={source_map[identity] for identity in expected.evidence_turn_ids if identity in source_map}
        sources.update(tool_map[tool.tool_call_id] for identity in expected.evidence_turn_ids for tool in turns[identity].tool_results if tool.tool_call_id in tool_map)
        sources=evidence_sources(sources,observed_tools)
        broad[expected.memory_id]=[card.item_id for card in cards if sources&({card.source_ref}|{ref.source_id for ref in card.evidence})]
    lines.append("## 类别识别候选（先按含义判断对应，再检查类别）")
    lines.extend(f"{identity} -> {'、'.join(values) or '无'}" for identity,values in broad.items())
    system=REVIEW_PROMPT+"\n候选来自同一冻结题目。你不知道开关组别、token成本或之前评分；独立逐条审核。violations只针对实际答案；卡片违规写入forbidden_card_ids。不能新增预期memory_id。合理但没有本题标准对应的卡片无需匹配，也不因此成为额外卡。额外返回classification_matches对象数组，每项包含memory_id与card_ids。只使用类别识别候选中语义完整对应的实际卡片，此数组允许类别错误的对应；不改变memory_matches的严格要求。"
    messages=[{"role":"system","content":system},{"role":"user","content":"\n".join(lines)}]
    for attempt in range(2):
        response=model.chat(messages,tools=None,max_tokens=2048,**structured_call_options(model))
        raw=response.text.strip()
        if raw.startswith("```"):raw="\n".join(raw.splitlines()[1:-1])
        try:
            result=_validate_review(json.loads(raw),allowed,{card.item_id for card in cards},answer)
            for item in result.get("classification_matches",[]):
                if item.get("memory_id") not in broad or not set(item.get("card_ids",[]))<=set(broad[item["memory_id"]]):raise ValueError("invalid classification match")
            return result,model.calls
        except (ValueError,TypeError) as exc:
            if attempt:raise
            messages.extend(({"role":"assistant","content":response.text},{"role":"user","content":f"评分结构/来源校验未通过：{exc}；重新独立核对，返回完整评分。"}))


def memory_metrics(case,question,cards,review,retriever,scope,refs=None):
    matched={identity for item in review.get("memory_matches",[]) for identity in item["card_ids"]}
    if refs is None:refs=[hit.reference.removeprefix("card:") for hit in retriever.search(scope,question.text,source="card",limit=10)]
    card_map={card.item_id:card for card in cards};expected={item.memory_id:item for item in case.expected_memories}
    classified={(item["memory_id"],identity) for item in review.get("classification_matches",[]) for identity in item["card_ids"]}
    correct=sum(card_map[identity].kind==expected[mid].kind for mid,identity in classified)
    end5=sum(bool(item["card_ids"]) and set(item["card_ids"])<=set(refs[:5]) for item in review.get("memory_matches",[]))
    end10=sum(bool(item["card_ids"]) and set(item["card_ids"])<=set(refs) for item in review.get("memory_matches",[]))
    return dict(expected_facts=len(question.expected_active_memory_ids or ()),matched_facts=len(review.get("matched_memory_ids",[])),cards=len(cards),matched_cards=len(matched),extra_card_ids=review.get("extra_card_ids",[]),forbidden_card_ids=review.get("forbidden_card_ids",[]),retrieval_expected_cards=len(matched),retrieval_at_5=len(matched&set(refs[:5])),retrieval_at_10=len(matched&set(refs)),end_to_end_at_5=end5,end_to_end_at_10=end10,retrieval_card_ids=refs,negative_case=not case.expected_memories,classification_correct=correct,classification_evaluated=len(classified),classification_missing="classification_matches" not in review)


def result_metadata(definition,case,question,repeat,name,out):
    return dict(case_id=case.case_id,question_id=question.question_id,question_hash=question.fingerprint(),dataset_id=definition.dataset_id,dataset_version=definition.version,repeat=repeat,condition=name,target=case.target,family=case.scenario,length=sum(len(history.turns) for history in case.history),high_noise="high_noise" in case.abilities,artifact_directory=str(out))


def save_ungraded(definition,case,question,repeat,name,out,answer,cards,data,scope,*,warm_step=None):
    value={**result_metadata(definition,case,question,repeat,name,out),**answer,"cards":[card.model_dump(mode="json") for card in cards],"source":data,"scope":scope.model_dump(mode="json"),"grade_status":"pending","passed":False}
    if warm_step:value["warm_step"]=warm_step
    atomic_json(out/"result.json",value)
    return value


def copy_database(source,destination):
    with closing(sqlite3.connect(source)) as before,closing(sqlite3.connect(destination)) as after:before.backup(after)


def run_case(definition,case,root,delegate,config,repeat):
    if (root/"manifest.json").exists():check_run(root)
    directory=root/"cases"/case.case_id/f"repeat-{repeat}"
    directory.mkdir(parents=True,exist_ok=True)
    scope=MemoryScope(tenant_id="benchmark120",user_id=f"{case.case_id}:r{repeat}")
    sources={}
    for memory in (False,True):
        seed_dir=directory/f"source-M{int(memory)}";seed_dir.mkdir(exist_ok=True)
        # 来源重放只在未完成检查点时运行；已完成检查点直接使用不变快照。
        seed=RuntimeStorage.open(seed_dir/"source.db")
        model=BenchmarkModel(delegate,seed_dir/"model-calls.jsonl",stage="extraction",tag=f"M{int(memory)}")
        replay=SourceReplayItem(seed,scope,model,memory=memory,directory=seed_dir)
        for question in case.questions:
            snapshot=seed_dir/f"{question.question_id}.db"
            mapping=seed_dir/f"{question.question_id}.json"
            if snapshot.exists() and mapping.exists():
                data=json.loads(mapping.read_text(encoding="utf-8"))
            else:
                # 续跑从已有事实ID重新构建映射，避免重复提交来源。
                if not replay.seen:
                    for old_question in case.questions:
                        old_map=seed_dir/f"{old_question.question_id}.json"
                        if old_map.exists():
                            old=json.loads(old_map.read_text(encoding="utf-8"));replay.source_map.update(old["source_map"]);replay.tool_map.update(old["tool_map"]);replay.seen.update(old["source_map"])
                session,previous=replay.advance(case,question)
                with seed.db.read() as conn:
                    jobs=[(row[0],json.loads(row[1] or "{}")) for row in conn.execute("SELECT status,result_json FROM jobs")]
                data=dict(source_map=replay.source_map.copy(),tool_map=replay.tool_map.copy(),source_issues=replay.source_issues.copy(),session_id=session.session_id,previous_session_ids=[item.session_id for item in previous],source_calls=copy.deepcopy(model.calls),extraction_complete=all(status=="completed" for status,_ in jobs) if memory else None,parse_clean=all(not result.get("review",{}).get("extraction_issues") for _,result in jobs) if memory else None)
                copy_database(seed.db.path,snapshot);atomic_json(mapping,data)
            sources[memory,question.question_id]=(snapshot,data)
    for question in case.questions:
            order=list(CONDITIONS);random.Random(config.random_seed+repeat+sum(map(ord,case.case_id+question.question_id))).shuffle(order)
            for perception,memory in order:
                if (root/"manifest.json").exists():check_run(root)
                snapshot,data=sources[memory,question.question_id]
                name=f"P{int(perception)}M{int(memory)}"
                out=directory/question.question_id/name;out.mkdir(parents=True,exist_ok=True)
                result_file=out/"result.json"
                if result_file.exists():continue
                store=RuntimeStorage.open(out/"agent.db") if (out/"agent.db").exists() and (out/"answer.json").exists() else None
                if store is None:
                    copy_database(snapshot,out/"agent.db");store=RuntimeStorage.open(out/"agent.db")
                current=store.sessions.get_session(data["session_id"])
                previous=tuple(store.sessions.get_session(identity) for identity in data["previous_session_ids"])
                answer_file=out/"answer.json"
                answer={}
                try:
                    if answer_file.exists():answer=json.loads(answer_file.read_text(encoding="utf-8"));retriever,_,_=components(store,out,scope,delegate,question,memory)
                    else:
                        answer,_,_,retriever=execute_answer(store,current,previous,scope,case,question,out,delegate,config,perception=perception,memory=memory)
                        atomic_json(answer_file,answer)
                    actual_data=copy.deepcopy(data)
                    if memory:actual_data["source_calls"].extend(finish_background(store,scope,out,delegate))
                    cards=all_cards(store,scope,question) if memory else ()
                    value=save_ungraded(definition,case,question,repeat,name,out,answer,cards,actual_data,scope)
                except Exception as exc:
                    calls_path=out/"model-calls.jsonl"
                    answer={**answer,"actual_answer":answer.get("actual_answer",""),"completed":False,"error":f"{type(exc).__name__}: {exc}","model_calls":[json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] if calls_path.exists() else [],"hard_checks":answer.get("hard_checks",{})}
                    value=save_ungraded(definition,case,question,repeat,name,out,answer,(),data,scope)
                atomic_json(result_file,value)
                print(f"[benchmark] {case.case_id}/{question.question_id}/r{repeat}/{name} recorded",flush=True)
    return directory


def reuse_definitions(definitions):
    selected=[]
    for target,per_family in (("perception",1),("combined",2)):
        source=next(definition for definition in definitions if definition.cases[0].target==target)
        cases=[]
        for family in sorted({case.scenario for case in source.cases}):
            candidates=sorted((case for case in source.cases if case.scenario==family),key=lambda case:case.case_id)
            for case in candidates[-per_family:]:
                base=case.questions[-1]
                openings=("","请用清单核对当前约定：","只采用当前有效信息再次回答：","向新协作者说明现状：","把已知与仍未知内容分开：","最后做一次一致性检查：")
                questions=tuple(base.model_copy(update={"question_id":f"w{i+1}","text":opening+base.text,"asked_at":base.asked_at+timedelta(minutes=10*i)}) for i,opening in enumerate(openings))
                cases.append(case.model_copy(update={"difficulty":"long_reuse","questions":questions}))
        selected.append(source.model_copy(update={"dataset_id":f"{target}_core_reuse","title":"连续六问Anchor复用评测","cases":tuple(cases)}))
    return tuple(selected)


def run_reuse_case(definition,case,root,delegate,config,repeat):
    if (root/"manifest.json").exists():check_run(root)
    base=root/"reuse"/case.case_id/f"repeat-{repeat}";seed_dir=base/"source";seed_dir.mkdir(parents=True,exist_ok=True)
    scope=MemoryScope(tenant_id="benchmark120-reuse",user_id=f"{case.case_id}:r{repeat}")
    seed=RuntimeStorage.open(seed_dir/"agent.db")
    initial_model=BenchmarkModel(delegate,seed_dir/"model-calls.jsonl",stage="extraction",tag="reuse-shared-memory")
    seed_replay=SourceReplayItem(seed,scope,initial_model,memory=True,directory=seed_dir)
    initial_path=seed_dir/"initial.json"
    if initial_path.exists():initial=json.loads(initial_path.read_text(encoding="utf-8"))
    else:
        session,previous=seed_replay.advance(case,case.questions[0])
        initial=dict(source_map=seed_replay.source_map,tool_map=seed_replay.tool_map,source_issues=seed_replay.source_issues,source_calls=initial_model.calls,session_id=session.session_id,previous_session_ids=[item.session_id for item in previous])
        copy_database(seed.db.path,seed_dir/"snapshot.db");atomic_json(initial_path,initial)
    order=[False,True];random.Random(config.random_seed+repeat+sum(map(ord,case.case_id))).shuffle(order)
    for perception in order:
        name=f"P{int(perception)}M1";directory=root/"reuse"/case.case_id/f"repeat-{repeat}"/name;directory.mkdir(parents=True,exist_ok=True)
        result_path=directory/"sequence.json"
        if result_path.exists():continue
        if not (directory/"agent.db").exists():copy_database(seed_dir/"snapshot.db",directory/"agent.db")
        storage=RuntimeStorage.open(directory/"agent.db")
        source_model=BenchmarkModel(delegate,directory/"source-calls.jsonl",stage="extraction",tag=name)
        replay=SourceReplayItem(storage,scope,source_model,memory=True,directory=directory)
        # 两组从同一memory快照分叉；六问保留各自真实会话和Anchor。
        session=storage.sessions.get_session(initial["session_id"])
        previous=tuple(storage.sessions.get_session(identity) for identity in initial["previous_session_ids"])
        initial_calls=initial["source_calls"]
        engine=None;model=BenchmarkModel(delegate,directory/"model-calls.jsonl",tag=name)
        rows=[]
        for i,question in enumerate(case.questions):
            if (root/"manifest.json").exists():check_run(root)
            out=directory/question.question_id;out.mkdir(exist_ok=True)
            if (out/"result.json").exists():
                rows.append(json.loads((out/"result.json").read_text(encoding="utf-8")));continue
            call_begin=len(source_model.calls)
            if (out/"answer.json").exists():answer=json.loads((out/"answer.json").read_text(encoding="utf-8"))
            else:
                answer,engine,_,retriever=execute_answer(storage,session,previous,scope,case,question,out,delegate,config,perception=perception,memory=True,engine=engine,model=model)
                atomic_json(out/"answer.json",answer)
            replay.worker.process_pending()
            cards=all_cards(storage,scope,question)
            data=dict(source_map=replay.source_map,tool_map=replay.tool_map,source_calls=(initial_calls if i==0 else [])+copy.deepcopy(source_model.calls[call_begin:]),session_id=session.session_id,previous_session_ids=[item.session_id for item in previous])
            value=save_ungraded(definition,case,question,repeat,name,out,answer,cards,data,scope,warm_step=i+1)
            atomic_json(out/"result.json",value);rows.append(value)
        flush_begin=len(source_model.calls)
        with storage.db.write() as conn:storage.review_scheduler.flush_scope(conn,scope.storage_key,trigger_type="reuse_complete")
        replay.worker.process_pending()
        rows[-1]["source"]["source_calls"].extend(copy.deepcopy(source_model.calls[flush_begin:]))
        atomic_json(directory/case.questions[-1].question_id/"result.json",rows[-1])
        atomic_json(result_path,dict(results=rows,source_calls=initial_calls+source_model.calls))
        print(f"[reuse] {case.case_id}/r{repeat}/{name} completed=6",flush=True)


def grade_results(root,definitions,delegate,config):
    lookup={(definition.dataset_id,case.case_id):(case,{q.question_id:q for q in case.questions}) for definition in definitions for case in definition.cases}
    paths=list((root/"cases").glob("**/result.json"))+list((root/"reuse").glob("**/result.json"))
    random.Random(config.random_seed).shuffle(paths)
    def grade(path):
        value=json.loads(path.read_text(encoding="utf-8"))
        if value.get("grade_status")=="completed":return
        case,questions=lookup[value["dataset_id"],value["case_id"]];question=questions[value["question_id"]]
        cards=tuple(UserMemoryItem.model_validate(card) for card in value["cards"])
        out=path.parent;source=value["source"]
        try:
            review,calls=blind_review(delegate,out/"review-calls.jsonl",case,question,value["actual_answer"],cards,source["source_map"],source["tool_map"],observed_tools=value.get("tool_calls",()))
            database=out/"agent.db" if not value.get("warm_step") else out.parent/"agent.db"
            store=RuntimeStorage.open(database);scope=MemoryScope.model_validate(value["scope"])
            retriever,_,_=components(store,out,scope,delegate,question,False)
            value.update(review=review,review_calls=calls,memory_metrics=memory_metrics(case,question,cards,review,retriever,scope,refs=value.get("retrieval_ids",[])) if value["condition"].endswith("M1") else None,grade_status="completed")
            value["passed"]=bool(value["completed"] and value["hard_checks"] and all(value["hard_checks"].values()) and review["answer_passed"] and not review["violations"] and not review["forbidden_card_ids"])
        except Exception as exc:
            value.update(grade_status="error",grading_error=f"{type(exc).__name__}: {exc}")
            if value["condition"].endswith("M1"):
                value["memory_metrics"]=memory_metrics(case,question,cards,{},None,None,refs=value.get("retrieval_ids",[]))
                value["memory_metrics"]["unscored_cards"]=len(cards)
        atomic_json(path,value)
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        for future in as_completed([pool.submit(grade,path) for path in paths]):future.result()
    strata={}
    for path in paths:
        value=json.loads(path.read_text(encoding="utf-8"));strata.setdefault((value["target"],value["length"],value["condition"]),[]).append(path)
    selected=set()
    rng=random.Random(config.random_seed+1)
    for members in strata.values():
        rng.shuffle(members);selected.update(members[:max(1,int(len(members)*config.review_fraction))])
    for path in paths:
        value=json.loads(path.read_text(encoding="utf-8"))
        if value.get("review",{}).get("violations") or value.get("review",{}).get("forbidden_card_ids") or value.get("grading_error") or not all(value.get("hard_checks",{}).values()):selected.add(path)
    def audit(path):
        value=json.loads(path.read_text(encoding="utf-8"))
        if "second_review" in value:return
        case,questions=lookup[value["dataset_id"],value["case_id"]];question=questions[value["question_id"]]
        source=value["source"];cards=tuple(UserMemoryItem.model_validate(card) for card in value["cards"])
        try:
            review,calls=blind_review(delegate,path.parent/"second-review-calls.jsonl",case,question,value["actual_answer"],cards,source["source_map"],source["tool_map"],review_index=1,observed_tools=value.get("tool_calls",()))
            value.update(second_review=review,second_review_calls=calls,review_disagreement=any(review.get(key)!=value.get("review",{}).get(key) for key in ("answer_passed","matched_memory_ids","forbidden_card_ids","extra_card_ids","violations")))
        except Exception as exc:value["second_review_error"]=type(exc).__name__
        atomic_json(path,value)
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        for future in as_completed([pool.submit(audit,path) for path in selected]):future.result()


def import_results(root,definitions,config):
    storage=RuntimeStorage.open(root/"evaluations.db")
    for definition in definitions:storage.evals.publish(definition)
    index_path=root/"import-index.json"
    index=json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    files=list((root/"cases").glob("**/result.json"))+list((root/"reuse").glob("**/result.json"))
    runs={}
    for path in files:
        stat=path.stat();signature=[stat.st_mtime_ns,stat.st_size];relative=str(path.relative_to(root))
        if index.get(relative)==signature:continue
        result=json.loads(path.read_text(encoding="utf-8"));dataset=result["dataset_id"]
        key=(dataset,result["condition"],result["repeat"])
        identity=uuid.uuid5(uuid.NAMESPACE_URL,str(root)+repr(key)).hex
        if key not in runs:
            definition=next(value for value in definitions if value.dataset_id==dataset)
            with storage.db.read() as conn:exists=conn.execute("SELECT 1 FROM eval_runs WHERE run_id=?",(identity,)).fetchone()
            if not exists:
                storage.evals.start_run(run_id=identity,dataset_id=dataset,dataset_version=definition.version,provider=result.get("model_calls",[{}])[0].get("provider","unknown") if result.get("model_calls") else "unknown",model=result.get("model_calls",[{}])[0].get("model","unknown") if result.get("model_calls") else "unknown",prompt_version=RUNNER_VERSION,extractor_version=MemoryExtractor.version,details=dict(condition=key[1],repeat=key[2],config=asdict(config),manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))))
            runs[key]=[]
        with storage.db.read() as conn:old=conn.execute("SELECT result_json FROM eval_results WHERE run_id=? AND case_id=? AND question_id=?",(identity,result["case_id"],result["question_id"])).fetchone()
        if old is None or {k:v for k,v in json.loads(old[0]).items() if k!="review_history"}!=result:storage.evals.put_result(identity,result["case_id"],result["question_id"],status="pending" if result.get("grade_status")!="completed" else "passed" if result["passed"] else "failed",result=result)
        index[relative]=signature
        runs[key].append(result)
    for definition in definitions:
        planned=sum(len(case.questions) for case in definition.cases)
        for condition in ("P0M1","P1M1") if definition.dataset_id.endswith("reuse") else ("P0M0","P0M1","P1M0","P1M1"):
            for repeat in range(1,config.repeats+1):
                identity=uuid.uuid5(uuid.NAMESPACE_URL,str(root)+repr((definition.dataset_id,condition,repeat))).hex
                with storage.db.read() as conn:
                    total,pending,passed=conn.execute("SELECT count(*),sum(status='pending'),sum(status='passed') FROM eval_results WHERE run_id=?",(identity,)).fetchone()
                if total==planned and not pending:storage.evals.finish_run(identity,dict(answers=total,passed=passed))
    atomic_json(index_path,index)
    return storage


def archive_code(root,manifest):
    """归档本次源代码，指纹以外保留可复现文件。"""
    import shutil
    files=[ROOT/path for path in manifest["production"]]
    files.extend(path for folder in (ROOT/"agent/eval",ROOT/"agent/types/eval") for path in folder.rglob("*.py"))
    for path in set(files):
        target=root/"code-snapshot"/path.relative_to(ROOT)
        if not target.exists():target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)


def preflight(definitions,config):
    """按真实组装器估算完整前缀；估算不作为实际usage或节省证据。"""
    costs=[]
    for definition in definitions:
        for case in definition.cases:
            for question in case.questions:
                session=Session();seq=0
                for history in case.history_for(question):
                    for declared in history.turns:
                        session.add_message(Message(role="user",content_text=declared.user_text,trajectory_id=declared.turn_id))
                        for observation in declared.tool_results:
                            request=ToolCallRequest(id=observation.provider_tool_call_id or observation.tool_call_id,name=observation.name,arguments=observation.arguments)
                            session.add_message(build_assistant_message("",[request]))
                            call=ToolCall(tool_call_id=observation.tool_call_id,provider_tool_call_id=request.id,session_id=session.session_id,trajectory_id=declared.turn_id,tool_name=request.name,arguments=request.arguments,status=observation.status,result_text=observation.result_text,error=observation.error)
                            session.tool_calls.append(call);session.add_message(build_tool_message(call))
                        session.add_message(Message(role="assistant",content_text=declared.assistant_text or "收到。"))
                session.add_message(Message(role="user",content_text=question.text))
                engine=FullInput(trace=PerceptionTrace(),config=PerceptionConfig(budget=ContextBudget(model_context_tokens=config.full_context_tokens,reserved_output_tokens=config.output_tokens)))
                built=engine.build(session,trace_id="preflight",iteration=1)
                if not protocol_pairs(built.messages):raise ValueError("preflight_protocol_invalid")
                costs.append(dict(case_id=case.case_id,question_id=question.question_id,estimated_input_tokens=built.metrics.tokens_used))
    return dict(checkpoints=len(costs),max_estimated_input_tokens=max(item["estimated_input_tokens"] for item in costs),full_input_budget=config.full_context_tokens-config.output_tokens,estimator="SafeEstimator，非usage",provider_window_validation="以实际API请求是否被接受验证",checkpoints_detail=costs)


def run(root,*,pilot=False,config=None,delegate=None,include_reuse=True,case_ids=None):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    config=config or BenchmarkConfig(repeats=1 if pilot else 3)
    definitions=load_definitions(pilot=pilot)
    if not pilot:
        from agent.eval.datasets.core_benchmark import FIXTURES
        release=json.loads((FIXTURES/"release.json").read_text(encoding="utf-8"))
        if release["dataset_hashes"]!={definition.dataset_id:definition.fingerprint() for definition in definitions}:raise ValueError("正式发布指纹与题库不一致")
    delegate=delegate or ModelRegistry.default().get()
    manifest=dict(runner_version=RUNNER_VERSION,production=production_fingerprints(),evaluation_hash=evaluation_fingerprint(),config=asdict(config),dataset_hashes={definition.dataset_id:definition.fingerprint() for definition in definitions},provider=getattr(delegate,"name","unknown"),model=getattr(delegate,"model_name","unknown"),endpoint_hash=hashlib.sha256(str(getattr(delegate,"base_url","")).encode()).hexdigest(),pilot=pilot,include_reuse=include_reuse and not pilot,case_ids=case_ids)
    path=root/"manifest.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8"))!=manifest:raise ValueError("实现、配置或题库改变；必须使用新运行目录")
    atomic_json(path,manifest)
    atomic_json(root/"production-freeze.json",manifest["production"])
    archive_code(root,manifest)
    if not (root/"preflight.json").exists():atomic_json(root/"preflight.json",preflight(definitions,config))
    jobs=[(definition,case,repeat) for definition in definitions for case in definition.cases if not case_ids or case.case_id in case_ids for repeat in range(1,config.repeats+1)]
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        futures=[pool.submit(run_case,definition,case,root,delegate,config,repeat) for definition,case,repeat in jobs]
        for future in as_completed(futures):
            future.result()
            if production_fingerprints()!=manifest["production"]:raise ValueError("生产代码在运行中改变，停止混合版本")
            atomic_json(root/"progress.json",dict(completed_answers=len(list((root/"cases").glob("**/result.json"))),planned_answers=len(jobs)*12))
            import_results(root,definitions,config)
    reuse=()
    if include_reuse and not pilot:
        reuse=reuse_definitions(definitions)
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            futures=[pool.submit(run_reuse_case,definition,case,root,delegate,config,repeat) for definition in reuse for case in definition.cases for repeat in range(1,config.repeats+1)]
            for future in as_completed(futures):future.result()
    grade_results(root,(*definitions,*reuse),delegate,config)
    import_results(root,(*definitions,*reuse),config)
    from agent.eval.core_benchmark_report import build_report
    build_report(root)
    atomic_json(root/"completed.json",dict(formal_answers=len(list((root/"cases").glob("**/result.json"))),reuse_answers=len(list((root/"reuse").glob("**/result.json"))),production_unchanged=production_fingerprints()==manifest["production"]))
    return root


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--pilot",action="store_true")
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--repeats",type=int)
    parser.add_argument("--no-reuse",action="store_true")
    parser.add_argument("--cases",nargs="*")
    args=parser.parse_args()
    config=BenchmarkConfig(repeats=args.repeats or (1 if args.pilot else 3),workers=args.workers)
    run(args.output,pilot=args.pilot,config=config,include_reuse=not args.no_reuse,case_ids=args.cases)


if __name__=="__main__":main()
