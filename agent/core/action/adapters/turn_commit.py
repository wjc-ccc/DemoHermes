"""卡片写入与 action 回执共享 commit_turn 的同一 SQLite 事务。"""
from ..storage.tool_store import ActionConflictError
from agent.types.action.execution import now_ms
from agent.types.tools import ToolCallResult


class ActionTurnCommitHook:
    def __init__(self, ledger):
        self.ledger = ledger

    def _pending(self, conn, session, turn):
        for call in session.tool_calls:
            if call.trajectory_id != turn.turn_id:
                continue
            evidence = call.action_evidence
            if evidence is None or not evidence.deferred_commit or evidence.replayed:
                continue
            item = self.ledger.get(evidence.ledger_entry_id, conn=conn)
            if item and item.phase == "prepared":
                yield call, item

    def before_commit(self, conn, session, turn):
        for call in session.tool_calls:
            evidence = call.action_evidence
            if (evidence is not None and turn.completed and not turn.interrupted and call.ok and call.trajectory_id == turn.turn_id
                and evidence.deferred_commit and not evidence.replayed):
                current = self.ledger.get(evidence.ledger_entry_id, conn=conn)
                if not current or current.phase != "prepared":
                    raise ActionConflictError("deferred_commit_lease_lost")
        for call, item in self._pending(conn, session, turn):
            if not turn.completed or turn.interrupted or not call.ok:
                continue
            submitted = call.pending_mutation
            prepared = ToolCallResult.model_validate(item.raw_output).pending_mutation
            if submitted is not None and submitted != prepared:
                raise ActionConflictError("deferred_command_changed_after_pre")
            claim = conn.execute("SELECT lease_token FROM action_resource_claims WHERE entry_id=?", (item.entry_id,)).fetchone()
            if (item.lease_token != call.action_evidence.lease_token or not claim
                or claim[0] != item.lease_token or not item.lease_expires_at or item.lease_expires_at <= now_ms()):
                raise ActionConflictError("deferred_commit_lease_lost")
            if turn.completed and not turn.interrupted:
                row = conn.execute("SELECT revision FROM memory_cards WHERE id=? AND scope_key=?",
                                   (item.effective_args["item_id"], item.scope_key)).fetchone()
                if not row or str(row[0]) != item.resource_version:
                    raise ActionConflictError("stale_precondition")

    def record_mutation(self, conn, call, card):
        evidence = call.action_evidence
        if evidence is None or not evidence.deferred_commit:
            return
        item = self.ledger.get(evidence.ledger_entry_id, conn=conn)
        receipt = {"item_id": card.item_id, "revision": card.revision, "content_hash": card.content_hash}
        updated = self.ledger.update(item, changes={"effect_state": "succeeded", "phase": "finished",
            "external_ref": f"card:{card.item_id}:{card.revision}", "lease_expires_at": None},
            event_type="turn_commit_confirmed", evidence=receipt, conn=conn)
        self.ledger.release(updated, conn=conn)

    def finish_turn(self, conn, session, turn):
        for call, item in self._pending(conn, session, turn):
            if item.lease_token != call.action_evidence.lease_token:
                continue
            # 有成功轮次但未应用的暂存，也不能冒充已落库。
            updated = self.ledger.update(item, changes={"effect_state": "failed", "phase": "finished",
                "lease_expires_at": None}, event_type="deferred_cancelled", conn=conn)
            self.ledger.release(updated, conn=conn)

