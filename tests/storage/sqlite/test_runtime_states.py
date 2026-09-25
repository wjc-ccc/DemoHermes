from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest

from agent.storage.sqlite import RuntimeStorage, RevisionConflictError, IntegrityError, LeaseLostError
from agent.core.memory import MemoryScope, UserMemoryItem, content_sha256
from agent.core.memory.session_workspace import SessionWorkspace, WorkspaceItem
from agent.core.memory.session_narrative import GoalContract, NarrativeItem, EvidenceRef
from agent.core.memory.session_state import SessionStateItem, Provenance
from agent.core.memory.types import TurnRecord, MemoryProposal
from agent.core.memory import SessionMemoryReviewer, MemoryValidator
from agent.core.memory.memory_layer import MemoryLayer
from agent.core.types import Message


def seed(runtime, route="route"):
    scope=MemoryScope(user_id="user")
    session=runtime.sessions.get_or_create(route,scope=scope)
    message=session.add_message(Message(role="user",content_text="Prepare payroll; do not pay"))
    scope=scope.model_copy(update={"session_id":session.session_id,"task_id":session.session_id})
    turn=TurnRecord(turn_id="turn-"+route,session_id=session.session_id,scope=scope,
        user_message_id=message.message_id,assistant_message_ids=(),tool_call_ids=(),
        started_at=1,finished_at=2,completed=True,interrupted=False,error=None)
    runtime.sessions.commit_turn(session,turn,scope=scope)
    return session,turn,message


def test_three_states_round_trip_and_working_set(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db")
    session,turn,msg=seed(r)
    sid=session.session_id
    first=WorkspaceItem(item_id="collect",session_id=sid,title="Collect",executor_ref="analyst")
    second=WorkspaceItem(item_id="review",session_id=sid,title="Review",dependencies=("collect",))
    contract=GoalContract(session_id=sid,goal_id="payroll",user_goal="Prepare payroll",
        non_goals=("Do not pay",),success_criteria=("Snapshot ready",),constraints=("Use exact ids",),
        source_message_id=msg.message_id,changed_by="user",change_reason="Initial request")
    workspace=r.workspaces.create(SessionWorkspace(session_id=sid,goal_id="payroll",items=(first,second)),contract=contract)
    assert workspace.contract_revision==1
    assert r.workspaces.get_item(sid,"collect").executor_ref=="analyst"
    value=r.state.put(SessionStateItem(item_id="state-1",session_id=sid,scope_key="company:a",
        key="amount",value_type="decimal",value="100.00",created_by="runtime",
        workspace_item_id="collect",provenance=Provenance(source_type="user_input",source_id=msg.message_id)),
        expected_revision=0)
    event=r.narrative.append(NarrativeItem(item_id="event-1",session_id=sid,event_type="observation",
        what_happened="Amount received",author="runtime",state_delta_refs=(value.ref,),
        evidence_refs=(EvidenceRef(source_type="message",source_id=msg.message_id),)))
    selected=r.narrative.build_working_set(sid,contract_revision=1,item_id="review",
                                           event_ids=(event.item_id,),state_refs=(value.ref,))
    assert selected.contract==contract and selected.current_item==second
    assert selected.events==(event,) and selected.state_items==(value,)
    invalid=r.state.invalidate(value.ref,reason="Source corrected",actor="user")
    assert invalid.revision==2
    assert r.state.get_current(sid,"company:a","amount").validity.value=="invalidated"
    assert r.state.get(value.ref)==value
    with pytest.raises(RevisionConflictError):
        r.state.invalidate(value.ref,reason="Stale",actor="user")
    with pytest.raises(IntegrityError):
        r.narrative.append(event.model_copy(update={"what_happened":"overwrite"}))
    with pytest.raises(RevisionConflictError):
        r.narrative.append_contract(contract,expected_revision=0)
    with pytest.raises(RevisionConflictError):
        r.workspaces.replace_plan(sid,(first,second),expected_revision=1,actor="user",reason="Stale")
    snapshot=r.observe.session_states(sid)
    assert snapshot["state"]["items"][0]["revision"]==2
    assert snapshot["narrative"]["total"]==2


def test_dag_and_cross_session_integrity_roll_back(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db")
    s,t,m=seed(r)
    other,_,foreign=seed(r,"other")
    a=WorkspaceItem(item_id="a",session_id=s.session_id,title="A",dependencies=("b",))
    b=WorkspaceItem(item_id="b",session_id=s.session_id,title="B",dependencies=("a",))
    for items in [(a,), (a,b), (a.model_copy(update={"session_id":other.session_id}),)]:
        with pytest.raises(IntegrityError):
            r.workspaces.create(SessionWorkspace(session_id=s.session_id,goal_id="g",items=items))
        assert r.workspaces.get(s.session_id) is None
    bad=GoalContract(session_id=s.session_id,goal_id="g",user_goal="goal",
        source_message_id=foreign.message_id,changed_by="user",change_reason="request")
    with pytest.raises(IntegrityError):
        r.workspaces.create(SessionWorkspace(session_id=s.session_id,goal_id="g"),contract=bad)
    assert r.workspaces.get(s.session_id) is None
    with pytest.raises(IntegrityError):
        r.state.put(SessionStateItem(item_id="v",session_id=s.session_id,scope_key="x",key="id",
            value_type="string",value="foreign",created_by="runtime",
            provenance=Provenance(source_type="user_input",source_id=foreign.message_id)),expected_revision=0)


def test_concurrent_card_evidence_updates_preserve_history(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db")
    item=r.memory.put(UserMemoryItem(item_id="m",type="preference",source_type="manual",source_ref="u",
        scope=MemoryScope(user_id="u"),content="Chinese",content_hash=content_sha256("Chinese"),
        normalized_key="language",source_authority="manual",keywords=("Chinese",)))
    def update(index):
        candidate=item.model_copy(update={"revision":2,"evidence":(EvidenceRef(source_type="message",source_id=str(index)),)})
        try:
            return r.memory.put(candidate,expected_revision=1)
        except RevisionConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[1,2]))
    assert sum(value is not None for value in results)==1
    history=r.memory.history("m")
    assert len(history)==2 and history[1].evidence==()
    assert len(history[0].evidence)==1
    assert r.memory.search(item.scope,"chinese")[0].revision==2


def test_generic_jobs_dedupe_lease_fencing_and_atomic_claim(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db")
    kwargs=dict(job_type="example",handler_version="v1",input={"x":1},dedupe_key="one",available_at=0)
    j=r.jobs.enqueue(**kwargs)
    assert r.jobs.enqueue(**kwargs).id==j.id
    with pytest.raises(IntegrityError):
        r.jobs.enqueue(**dict(kwargs,input={"x":2}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda worker:r.jobs.claim(job_type="example",worker_id=worker,now=10,lease_ms=10),["a","b"]))
    old=next(c for c in claims if c)
    assert sum(c is not None for c in claims)==1
    fresh=r.jobs.claim(job_type="example",worker_id="new",now=20,lease_ms=10)
    with pytest.raises(LeaseLostError):
        r.jobs.complete(old,{"ok":True},now=21)
    with pytest.raises(LeaseLostError):
        r.jobs.fail(old,"stale",available_at=25,now=21)
    done=r.jobs.complete(fresh,{"ok":True},now=21)
    assert r.jobs.complete(fresh,{"ok":True},now=22)==done


class ObservationExtractor:
    def extract(self,turn,session):
        return [MemoryProposal(proposal_id="p",target_layer=MemoryLayer.TASK,type="goal",
            normalized_key="task.goal",summary="Prepare",content="Prepare payroll",confidence=0.9,
            source_authority="user_explicit",source_message_ids=(turn.user_message_id,),reason_code="user_goal")]


def test_extraction_only_appends_observation_and_fences_duplicate_submit(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db",extractor_version="v1")
    s,t,m=seed(r)
    job=r.jobs.claim(job_type="memory.extract",worker_id="w",lease_ms=100000)
    result=SessionMemoryReviewer(ObservationExtractor()).review(t,s)
    committed=r.review_committer.commit(job,result,extractor_version="v1")
    assert committed.narrative_item_count==1
    assert r.review_committer.commit(job,result,extractor_version="v1").idempotent
    assert r.workspaces.get(s.session_id) is None
    assert r.state.list_current(s.session_id).items==()
    events=r.narrative.list_events(s.session_id)
    assert len(events)==1 and events[0].status=="proposed" and events[0].evidence_refs
    assert events[0].metadata["model_claim"]["confidence"]==0.9


def test_review_trace_failure_rolls_back_all_results(tmp_path):
    r=RuntimeStorage.open(tmp_path/"agent.db",extractor_version="v1")
    s,t,m=seed(r)
    job=r.jobs.claim(job_type="memory.extract",worker_id="w")
    result=SessionMemoryReviewer(ObservationExtractor()).review(t,s)
    # A duplicate trace forces a failure after the narrative append.
    r.traces.put(result.trace)
    with pytest.raises(sqlite3.IntegrityError):
        r.review_committer.commit(job,result,extractor_version="v1")
    assert r.narrative.list_events(s.session_id)==()
    assert r.jobs.get(job.id).status=="running"


def test_observe_reads_do_not_change_database(tmp_path):
    path=tmp_path/"agent.db"
    r=RuntimeStorage.open(path)
    s,_,_=seed(r)
    with r.db.read() as conn:
        before=conn.execute("SELECT total_changes()").fetchone()[0]
        snapshot=tuple(conn.iterdump())
    assert len(r.observe.catalog()["databases"][0]["tables"])==17
    r.observe.table_page("sessions")
    r.observe.session_states(s.session_id)
    with r.db.read() as conn:
        assert tuple(conn.iterdump())==snapshot
        assert conn.execute("SELECT total_changes()").fetchone()[0]==before
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM sessions")

def test_card_and_history_rollback_when_review_commit_fails(tmp_path):
    class CardExtractor:
        def extract(self,turn,session):
            return [MemoryProposal(proposal_id="p",target_layer=MemoryLayer.USER,type="preference",
                normalized_key="language",summary="Chinese",content="Chinese",confidence=0.9,
                source_authority="user_explicit",source_message_ids=(turn.user_message_id,),reason_code="preference")]
    r=RuntimeStorage.open(tmp_path/"agent.db",extractor_version="v1")
    s,t,m=seed(r)
    job=r.jobs.claim(job_type="memory.extract",worker_id="w")
    result=SessionMemoryReviewer(CardExtractor()).review(t,s)
    r.traces.put(result.trace)
    with pytest.raises(sqlite3.IntegrityError):
        r.review_committer.commit(job,result,extractor_version="v1")
    with r.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM memory_item_versions").fetchone()[0]==0
    assert r.jobs.get(job.id).status=="running"


def test_skill_replay_uses_generic_job_results(tmp_path):
    from agent.evolution.skill_evolution import SkillReplayWorker, suite_for_skill
    class Evaluator:
        def __init__(self):
            self.calls=[]
        def evaluate_and_commit(self,name,suite,patch=None):
            self.calls.append((name,suite,patch))
            return True
    r=RuntimeStorage.open(tmp_path/"agent.db")
    evaluator=Evaluator()
    worker=SkillReplayWorker(evaluator,r.jobs)
    suite=suite_for_skill(skill_name="example",user_text="Create report")
    worker.submit("example",suite)
    worker.submit("example",suite)
    assert len(r.jobs.list(job_type="skill.evaluate"))==1
    assert worker.process_pending()==1
    assert worker.process_pending()==0
    job=r.jobs.list(job_type="skill.evaluate")[0]
    assert job.status=="completed" and job.result=={"admitted":True}
    assert evaluator.calls==[("example",suite,None)]
