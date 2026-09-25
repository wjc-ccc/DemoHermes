"""Commit memory, proposed observations, trace and job completion in one transaction."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from ...core.memory.memory_trace import MemoryItemTrace, MemoryTraceDecision, MemoryTraceOperation
from ...core.memory.session_memory_reviewer import MemoryReviewResult
from ...core.memory.session_narrative import EvidenceRef
from ...core.memory.types import ProposedMutation
from ...core.memory.memory_item import UserMemoryItem, UserMemoryStatus
from .codec import milliseconds, utcnow
from .errors import IntegrityError, LeaseLostError
from .job_store import read_job, require_lease, complete_job
from .memory_store import put_user_memory
from .narrative_store import append_event
from .trace_store import put_trace


@dataclass(frozen=True)
class MemoryReviewCommitResult:
    job_id: str
    trace_id: str
    user_item_count: int
    narrative_item_count: int
    idempotent: bool = False


class SqliteMemoryReviewCommitter:
    def __init__(self, db):
        self.db = db

    def commit(self, job, result: MemoryReviewResult, *, extractor_version: str, now=None):
        now_ms = milliseconds() if now is None else now
        request_hash = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
        with self.db.write() as conn:
            current = read_job(conn,job.id)
            if current is None or current.job_type != "memory.extract" or current.handler_version != extractor_version:
                raise IntegrityError("review handler does not match job")
            if current.status == "completed":
                if not job.lease_token or current.lease_token != job.lease_token or current.result["request_hash"] != request_hash:
                    raise LeaseLostError("review already committed with another claim or result")
                summary = current.result["summary"]
                return MemoryReviewCommitResult(**summary,idempotent=True)
            require_lease(conn,job,now_ms)
            if result.trace.session_id != current.session_id or result.trace.turn_id != current.turn_id:
                raise IntegrityError("review result belongs to another turn")
            proposals={p.proposal_id:p for p in result.proposals}
            traces={t.proposal_id:t for t in result.trace.items}
            evidence={}
            for e in result.evidence:
                evidence.setdefault(e.proposal_id,[]).append(
                    EvidenceRef(**e.model_dump(exclude={"proposal_id"})))
            users=[]
            for item in result.user_items:
                pid=str(item.metadata["proposal_id"])
                proposal=proposals[pid]
                item=item.model_copy(update={"evidence":tuple(evidence.get(pid,()))})
                candidates=self._find_user_candidates(conn,item)
                exact=next((c for c in candidates if c.content_hash==item.content_hash),None)
                if exact:
                    merged={e.model_dump_json():e for e in (*exact.evidence,*item.evidence)}
                    keywords=tuple(dict.fromkeys((*exact.keywords,*item.keywords)))
                    if tuple(merged.values()) != exact.evidence or keywords != exact.keywords:
                        exact=put_user_memory(conn,exact.model_copy(update={
                            "evidence":tuple(merged.values()),"keywords":keywords,"revision":exact.revision+1}),
                            expected_revision=exact.revision)
                    users.append(exact)
                    traces[pid]=self._with_resolution(traces[pid],item_id=exact.item_id,
                        decision=MemoryTraceDecision.SKIPPED,dedup_ids=(exact.item_id,),
                        validation_code="duplicate_existing")
                    continue
                if candidates:
                    previous=next((c for c in candidates if proposal.proposed_mutation is ProposedMutation.SUPERSEDE
                                   and proposal.supersedes_item_id==c.item_id),None)
                    if previous is None:
                        traces[pid]=self._with_resolution(traces[pid],item_id=None,
                            decision=MemoryTraceDecision.REJECTED,conflict_ids=tuple(c.item_id for c in candidates),
                            validation_code="conflict_requires_resolution")
                        continue
                    put_user_memory(conn,previous.model_copy(update={
                        "status":UserMemoryStatus.SUPERSEDED,"revision":previous.revision+1}),
                        expected_revision=previous.revision)
                    traces[pid]=self._with_resolution(traces[pid],item_id=item.item_id,
                        decision=MemoryTraceDecision.SUPERSEDED,conflict_ids=(previous.item_id,),
                        validation_code="superseded_existing")
                users.append(put_user_memory(conn,item))
            events=[]
            for item in result.narrative_items:
                if item.session_id != current.session_id or item.status != "proposed":
                    raise IntegrityError("extraction can only append proposed observations in its session")
                pid=str(item.metadata["proposal_id"])
                events.append(append_event(conn,item.model_copy(update={"evidence_refs":tuple(evidence.get(pid,()))})))
            trace=result.trace.model_copy(update={"operation":MemoryTraceOperation.PERSIST,
                "job_id":job.id,"items":tuple(traces.get(t.proposal_id,t) for t in result.trace.items)})
            put_trace(conn,trace)
            summary=dict(job_id=job.id,trace_id=trace.trace_id,user_item_count=len(users),narrative_item_count=len(events))
            persisted=result.model_copy(update={"user_items":tuple(users),"narrative_items":tuple(events),"trace":trace})
            complete_job(conn,job,dict(request_hash=request_hash,summary=summary,
                                      review=persisted.model_dump(mode="json")),now=now_ms)
            return MemoryReviewCommitResult(**summary)

    @staticmethod
    def _find_user_candidates(conn, item: UserMemoryItem) -> tuple[UserMemoryItem, ...]:
        scope = item.scope
        rows = conn.execute(
            "SELECT content_json FROM memory_items "
            "WHERE organization_id IS ? AND tenant_id IS ? AND user_id IS ? "
            "AND project_id IS ? AND normalized_key = ? "
            "AND status IN ('proposed', 'active') ORDER BY updated_at DESC, id",
            (
                scope.organization_id,
                scope.tenant_id,
                scope.user_id,
                scope.project_id,
                item.normalized_key,
            ),
        ).fetchall()
        return tuple(UserMemoryItem.model_validate_json(row[0]) for row in rows)

    @staticmethod
    def _with_resolution(
        trace_item: MemoryItemTrace,
        *,
        item_id: str | None,
        decision: MemoryTraceDecision,
        validation_code: str,
        dedup_ids: tuple[str, ...] = (),
        conflict_ids: tuple[str, ...] = (),
    ) -> MemoryItemTrace:
        runtime = trace_item.runtime
        return trace_item.model_copy(
            update={
                "item_id": item_id,
                "decision": decision,
                "runtime": runtime.model_copy(
                    update={
                        "dedup_candidate_ids": dedup_ids,
                        "conflict_item_ids": conflict_ids,
                        "validation_codes": (
                            *runtime.validation_codes,
                            validation_code,
                        ),
                    }
                ),
            }
        )
