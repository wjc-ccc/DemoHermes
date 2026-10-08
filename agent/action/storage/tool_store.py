"""短 SQLite 事务：业务去重、尝试、CAS、资源占用和证据在一个入口维护。"""
import uuid
from ..ledger import ActionItem, ActionAttemptItem, canonical, now_ms


class ActionConflictError(ValueError):
    pass


class SqliteToolLedgerStore:
    def __init__(self, database):
        self.db = database

    def get(self, entry_id, *, conn=None):
        if conn is None:
            with self.db.read() as connection:
                return self.get(entry_id, conn=connection)
        row = conn.execute("SELECT payload_json FROM action_items WHERE entry_id=?", (entry_id,)).fetchone()
        return ActionItem.model_validate_json(row[0]) if row else None

    def get_by_key(self, key):
        with self.db.read() as conn:
            row = conn.execute("SELECT payload_json FROM action_items WHERE idempotency_key=?", (key,)).fetchone()
        return ActionItem.model_validate_json(row[0]) if row else None

    def list(self, *, scope_key=None, step_id=None, plan_id=None, unfinished=False):
        with self.db.read() as conn:
            rows = conn.execute("SELECT payload_json FROM action_items ORDER BY created_at,entry_id").fetchall()
            bindings = None
            if step_id is not None or plan_id is not None:
                clauses, values = [], []
                for field, value in (("step_id", step_id), ("plan_id", plan_id)):
                    if value is not None:
                        clauses.append("json_extract(t.context_json,'$." + field + "')=?")
                        values.append(value)
                bindings = {entry_id: context_json for entry_id, context_json in conn.execute(
                    "SELECT a.entry_id,t.context_json FROM action_attempts a JOIN action_turns t "
                    "ON a.scope_key=t.scope_key AND a.turn_id=t.turn_id WHERE " + " AND ".join(clauses)
                    + " ORDER BY a.rowid", values)}
        items = tuple(ActionItem.model_validate_json(row[0]) for row in rows)
        if bindings is not None:
            import json
            # 同一业务动作可被多个计划/步骤引用；绑定来自每次尝试的 turn 快照。
            items = tuple(item.model_copy(update={"context": json.loads(bindings[item.entry_id])}) for item in items
                          if item.entry_id in bindings)
        return tuple(item for item in items
                     if (scope_key is None or item.scope_key == scope_key)
                     and (step_id is None or item.context.get("step_id") == step_id)
                     and (plan_id is None or item.context.get("plan_id") == plan_id)
                     and (not unfinished or item.effect_state in {"started", "unknown"}))

    def attempts(self, entry_id=None):
        with self.db.read() as conn:
            rows = conn.execute("SELECT payload_json FROM action_attempts "
                                + ("WHERE entry_id=? " if entry_id else "")
                                + "ORDER BY created_at,attempt_id", (entry_id,) if entry_id else ()).fetchall()
        return tuple(ActionAttemptItem.model_validate_json(row[0]) for row in rows)

    def begin_attempt(self, request, context):
        attempt = ActionAttemptItem(
            attempt_id=uuid.uuid4().hex, tool_call_id=request.id or uuid.uuid4().hex,
            tool_name=request.name, scope_key=context.scope_key, turn_id=context.turn_id,
            requested_args=request.arguments,
        )
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                attempt.attempt_id, None, attempt.tool_call_id, attempt.tool_name,
                attempt.scope_key, attempt.turn_id, 0, attempt.state, attempt.created_at,
                attempt.model_dump_json(),
            ))
        return attempt

    def finish_attempt(self, attempt, *, entry_id=None, state, reason=None, admitted=False, conn=None):
        if conn is None:
            with self.db.write() as connection:
                return self.finish_attempt(attempt, entry_id=entry_id, state=state, reason=reason,
                                           admitted=admitted, conn=connection)
        updated = attempt.model_copy(update={"entry_id": entry_id, "state": state, "reason": reason,
                                              "admitted": admitted, "finished_at": now_ms()})
        conn.execute("UPDATE action_attempts SET entry_id=?,admitted=?,state=?,payload_json=? WHERE attempt_id=?",
                     (entry_id, int(admitted), state, updated.model_dump_json(), attempt.attempt_id))
        return updated

    def _insert(self, conn, item):
        conn.execute("INSERT INTO action_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            item.entry_id, item.idempotency_key, item.scope_key, item.tool_name, item.effect_state,
            item.phase, item.publish_state, item.revision, item.resource_ref, item.lease_expires_at,
            item.created_at, item.updated_at, item.model_dump_json(),
        ))
        self._event(conn, item, "created", {})

    def _event(self, conn, item, event_type, evidence):
        conn.execute("INSERT INTO action_events VALUES (?,?,?,?,?,?)", (
            uuid.uuid4().hex, item.entry_id, item.revision, event_type, now_ms(), canonical(evidence),
        ))

    def update(self, item, *, changes, event_type, evidence=None, conn=None):
        if conn is None:
            with self.db.write() as connection:
                return self.update(item, changes=changes, event_type=event_type, evidence=evidence, conn=connection)
        current = self.get(item.entry_id, conn=conn)
        if current is None or current.revision != item.revision:
            raise ActionConflictError("revision_conflict")
        # 成功副作用的历史不允许被失败、重试或发布拦截覆盖。
        if current.effect_state == "succeeded" and changes.get("effect_state", "succeeded") != "succeeded":
            raise ActionConflictError("succeeded_is_immutable")
        allowed = {"not_started": {"not_started", "started", "failed"}, "started": {"started", "succeeded", "failed", "unknown"},
                   "unknown": {"unknown", "succeeded", "failed"}, "failed": {"failed", "started"}, "succeeded": {"succeeded"}}
        next_state = changes.get("effect_state", current.effect_state)
        late_preparation = current.effect_state == "unknown" and next_state == "started" and changes.get("phase") == "prepared"
        if next_state not in allowed[current.effect_state] and not late_preparation:
            raise ActionConflictError("invalid_state_transition")
        if current.compensation_state == "succeeded" and changes.get("compensation_state", "succeeded") != "succeeded":
            raise ActionConflictError("compensation_success_is_immutable")
        updated = current.model_copy(update={**changes, "revision": current.revision + 1, "updated_at": now_ms()})
        cursor = conn.execute("UPDATE action_items SET effect_state=?,phase=?,publish_state=?,revision=?,"
                              "lease_expires_at=?,updated_at=?,payload_json=? WHERE entry_id=? AND revision=?", (
            updated.effect_state, updated.phase, updated.publish_state, updated.revision,
            updated.lease_expires_at, updated.updated_at, updated.model_dump_json(), updated.entry_id, item.revision,
        ))
        if cursor.rowcount != 1:
            raise ActionConflictError("revision_conflict")
        self._event(conn, updated, event_type, evidence or {})
        return updated

    def block(self, item, attempt, reason):
        blocked = item.model_copy(update={"idempotency_key": "blocked:" + attempt.attempt_id,
                                         "phase": "blocked_pre"})
        with self.db.write() as conn:
            self._insert(conn, blocked)
            self.finish_attempt(attempt, entry_id=blocked.entry_id, state="blocked_pre", reason=reason, conn=conn)
        return blocked

    def reserve(self, candidate, attempt, *, max_calls, max_attempts, lease_ms, validate_context=None):
        """返回 (动作, 是否获得发送权)；数据库事务覆盖预算和资源争用。"""
        with self.db.write() as conn:
            if validate_context is not None:
                validate_context(conn)
            used = conn.execute("SELECT COUNT(*) FROM action_attempts WHERE scope_key=? AND turn_id=? "
                                "AND tool_name=? AND admitted=1", (
                attempt.scope_key, attempt.turn_id, attempt.tool_name,
            )).fetchone()[0]
            if used >= max_calls:
                raise ActionConflictError("call_budget_exhausted")
            row = conn.execute("SELECT entry_id FROM action_items WHERE idempotency_key=?",
                               (candidate.idempotency_key,)).fetchone()
            item = self.get(row[0], conn=conn) if row else candidate
            if row and item.input_hash != candidate.input_hash:
                raise ActionConflictError("idempotency_parameter_conflict")
            if not row:
                self._insert(conn, item)
            if item.effect_state in {"succeeded", "started", "unknown"}:
                self.finish_attempt(attempt, entry_id=item.entry_id, state="replay" if item.effect_state == "succeeded"
                                    else "in_flight", admitted=True, conn=conn)
                return item, False
            if item.attempt_count >= max_attempts:
                raise ActionConflictError("attempt_budget_exhausted")
            if item.phase == "blocked_pre":
                raise ActionConflictError("blocked_pre")
            token = uuid.uuid4().hex
            if item.resource_ref and item.effect != "read":
                claim = conn.execute("SELECT entry_id FROM action_resource_claims WHERE scope_key=? AND resource_ref=?",
                                     (item.scope_key, item.resource_ref)).fetchone()
                if claim and claim[0] != item.entry_id:
                    raise ActionConflictError("resource_in_flight")
                conn.execute("INSERT INTO action_resource_claims VALUES (?,?,?,?) "
                             "ON CONFLICT(scope_key,resource_ref) DO UPDATE SET lease_token=excluded.lease_token",
                             (item.scope_key, item.resource_ref, item.entry_id, token))
            item = self.update(item, changes={
                "effect_state": "started", "phase": "pending", "attempt_count": item.attempt_count + 1,
                "lease_owner": candidate.lease_owner, "lease_token": token,
                "lease_expires_at": now_ms() + lease_ms, "pre_decision": candidate.pre_decision,
                "context": candidate.context,
            }, event_type="dispatch_intent", conn=conn)
            self.finish_attempt(attempt, entry_id=item.entry_id, state="started", admitted=True, conn=conn)
            return item, True

    def release(self, item, *, conn=None):
        if conn is None:
            with self.db.write() as connection:
                return self.release(item, conn=connection)
        conn.execute("DELETE FROM action_resource_claims WHERE entry_id=? AND lease_token=?",
                     (item.entry_id, item.lease_token))

    def observe(self, attempt, context, resource_ref, version):
        if not resource_ref or version is None:
            return
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_observations VALUES (?,?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, context.scope_key, context.turn_id, context.step_id,
                resource_ref, str(version), attempt.attempt_id, now_ms(), context.plan_id,
            ))

    def has_observation(self, context, resource_ref, version):
        with self.db.read() as conn:
            row = conn.execute("SELECT 1 FROM action_observations WHERE scope_key=? AND resource_ref=? "
                               "AND resource_version=? AND (turn_id=? OR (? IS NOT NULL AND step_id=? AND plan_id=?)) LIMIT 1", (
                context.scope_key, resource_ref, str(version), context.turn_id, context.step_id, context.step_id, context.plan_id,
            )).fetchone()
        return row is not None

    def freeze_contracts(self, context, contracts):
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_turns VALUES (?,?,?)", (
                context.scope_key, context.turn_id, context.model_dump_json()))
            for name, contract in contracts.items():
                conn.execute("INSERT INTO action_contracts VALUES (?,?,?,?,?)", (
                    context.scope_key, context.turn_id, name, context.contract_hashes[name], canonical(contract),
                ))

    def approve(self, *, scope_key, tool_name, binding_hash, actor_id, expires_at):
        if not actor_id or expires_at <= now_ms():
            raise ValueError("审批需要可信审批人和未来有效期")
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_approvals VALUES (?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, scope_key, tool_name, binding_hash, actor_id, expires_at, now_ms(),
            ))

    def approved(self, context, tool_name, binding_hash):
        with self.db.read() as conn:
            return conn.execute("SELECT 1 FROM action_approvals WHERE scope_key=? AND tool_name=? "
                                "AND binding_hash=? AND expires_at>?", (
                context.scope_key, tool_name, binding_hash, now_ms(),
            )).fetchone() is not None

    def events(self, entry_id):
        with self.db.read() as conn:
            return tuple(conn.execute("SELECT event_type,payload_json FROM action_events WHERE entry_id=? "
                                      "ORDER BY rowid", (entry_id,)).fetchall())
