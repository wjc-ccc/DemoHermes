"""在一个事务中提交卡片、Trace 与提炼作业状态。

职责：合并重复卡、裁决同槽位更正和冲突，并保存可追溯证据。
边界：只处理 Reviewer 已验证的结果；不调用模型，也不负责提炼调度。
关联：MemoryReviewWorker 调用；memory_store.py 和 trace_store.py 执行具体写入。
"""
from __future__ import annotations
import hashlib
from agent.types.memory.trace import MemoryItemTrace
from agent.types.memory.trace_categories import MemoryTraceDecision, MemoryTraceOperation
from agent.types.memory.extraction import MemoryReviewResult
from agent.types.memory.source import EvidenceRef
from agent.types.memory.items import UserMemoryItem
from agent.types.memory.categories import UserCardKind, UserCardStatus
from .codec import milliseconds
from .errors import IntegrityError, LeaseLostError
from .job_store import read_job, require_lease, complete_job
from .memory_store import put_user_memory, query_user_memories
from .trace_store import put_trace
from .memory_card_policy import (
    can_restore_after_delete,
    stronger_or_newer,
    is_explicit_correction,
    merge_card_evidence,
)

from agent.types.storage import MemoryReviewCommitResult


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
            session_row=conn.execute("SELECT scope_key FROM sessions WHERE session_id=?",
                (current.session_id,)).fetchone()
            if session_row is None:
                raise IntegrityError("review job session missing")
            job_scope_key=session_row[0]
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
                if (item.scope.storage_key != job_scope_key or not
                    self._proposal_sources_in_scope(conn,job_scope_key,proposal)):
                    raise IntegrityError("user memory evidence outside review scope")
                item=item.model_copy(update={"evidence":tuple(evidence.get(pid,()))})
                candidates=self._find_user_candidates(conn,item)
                tombstones=tuple(c for c in candidates if c.status is UserCardStatus.DELETED)
                if tombstones and not can_restore_after_delete(item,tombstones):
                    traces[pid]=self._with_resolution(traces[pid],item_id=None,
                        decision=MemoryTraceDecision.REJECTED,
                        conflict_ids=tuple(c.item_id for c in tombstones),
                        validation_code="deleted_fact_cannot_be_restored_from_old_evidence")
                    continue
                live=tuple(c for c in candidates if c.status is UserCardStatus.ACTIVE)
                exact=next((c for c in live if c.content_hash==item.content_hash),None)
                if exact:
                    merged, keywords = merge_card_evidence(exact, item)
                    if merged != exact.evidence or keywords != exact.keywords:
                        exact=put_user_memory(conn,exact.model_copy(update={
                            "evidence":merged,"keywords":keywords,
                            "revision":exact.revision+1}),
                            expected_revision=exact.revision)
                    users.append(exact)
                    traces[pid]=self._with_resolution(traces[pid],item_id=exact.item_id,
                        decision=MemoryTraceDecision.SKIPPED,dedup_ids=(exact.item_id,),
                        validation_code="duplicate_existing")
                    continue
                if live:
                    if any(stronger_or_newer(c,item) for c in live):
                        conn.execute("INSERT INTO memory_conflicts("
                            "scope_key,slot_key,incumbent_id,candidate_json,reason,created_at) "
                            "VALUES (?,?,?,?,?,?)", (
                                item.scope.storage_key, item.slot_key or "experience",
                                live[0].item_id, item.model_dump_json(),
                                "stale_or_weaker_evidence", now_ms))
                        traces[pid]=self._with_resolution(traces[pid],item_id=None,
                            decision=MemoryTraceDecision.REJECTED,
                            conflict_ids=tuple(c.item_id for c in live),
                            validation_code="existing_fact_has_stronger_or_newer_evidence")
                        continue
                    if is_explicit_correction(item,live):
                        for previous in live:
                            put_user_memory(conn,previous.model_copy(update={
                                "status":UserCardStatus.SUPERSEDED,
                                "revision":previous.revision+1}),
                                expected_revision=previous.revision)
                        item=item.model_copy(update={
                            "supersedes_item_id":live[0].item_id})
                        traces[pid]=self._with_resolution(traces[pid],item_id=item.item_id,
                            decision=MemoryTraceDecision.SUPERSEDED,
                            conflict_ids=tuple(c.item_id for c in live),
                            validation_code="newer_explicit_correction")
                    else:
                        conn.execute("INSERT INTO memory_conflicts("
                            "scope_key,slot_key,incumbent_id,candidate_json,reason,created_at) "
                            "VALUES (?,?,?,?,?,?)", (
                                item.scope.storage_key, item.slot_key or "experience",
                                live[0].item_id, item.model_dump_json(),
                                "unresolved_inference", now_ms))
                        traces[pid]=self._with_resolution(traces[pid],item_id=None,
                            decision=MemoryTraceDecision.REJECTED,
                            conflict_ids=tuple(c.item_id for c in live),
                            validation_code="unresolved_conflict_kept_existing")
                        continue
                users.append(put_user_memory(conn,item))
            trace=result.trace.model_copy(update={"operation":MemoryTraceOperation.PERSIST,
                "job_id":job.id,"items":tuple(traces.get(t.proposal_id,t) for t in result.trace.items)})
            put_trace(conn,trace)
            summary=dict(job_id=job.id,trace_id=trace.trace_id,user_item_count=len(users))
            persisted=result.model_copy(update={"user_items":tuple(users),"trace":trace})
            complete_job(conn,job,dict(request_hash=request_hash,summary=summary,
                                      review=persisted.model_dump(mode="json")),now=now_ms)
            return MemoryReviewCommitResult(**summary)

    @staticmethod
    def _find_user_candidates(conn, item: UserMemoryItem) -> tuple[UserMemoryItem, ...]:
        if item.kind is UserCardKind.REUSABLE_EXPERIENCE:
            where = "scope_key=? AND kind='reusable_experience' AND content_hash=?"
            values = (item.scope.storage_key, item.content_hash)
        else:
            where = "scope_key=? AND slot_key=?"
            values = (item.scope.storage_key, item.slot_key)
        return query_user_memories(conn, where + " AND status IN ('active','deleted')", values)

    @staticmethod
    def _proposal_sources_in_scope(conn, scope_key, proposal):
        for table, identity_column, source_ids in (
            ("messages", "message_id", tuple(set(proposal.source_message_ids))),
            ("tool_calls", "tool_call_id", tuple(set(proposal.source_tool_call_ids))),
        ):
            if not source_ids:
                continue
            placeholders=",".join("?" for _ in source_ids)
            rows=conn.execute(
                "SELECT s.scope_key FROM "+table+" f JOIN sessions s "
                "ON s.session_id=f.session_id WHERE f."+identity_column+" IN ("+placeholders+")",
                source_ids).fetchall()
            if len(rows)!=len(source_ids) or any(row[0]!=scope_key for row in rows):
                return False
        return bool(proposal.source_message_ids or proposal.source_tool_call_ids)

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
