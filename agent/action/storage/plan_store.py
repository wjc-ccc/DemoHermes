import uuid
from ..ledger import now_ms
from ..plan.plan import Plan
from .tool_store import ActionConflictError


class SqlitePlanStore:
    def __init__(self, database):
        self.db = database

    def get(self, plan_id, *, conn=None):
        if conn is None:
            with self.db.read() as connection:
                return self.get(plan_id, conn=connection)
        row = conn.execute("SELECT payload_json FROM action_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return Plan.model_validate_json(row[0]) if row else None

    def _write_steps(self, conn, plan):
        for step in plan.steps:
            conn.execute("INSERT INTO action_steps VALUES (?,?,?,?,?,?) ON CONFLICT(plan_id,step_id) "
                         "DO UPDATE SET status=excluded.status,lease_token=excluded.lease_token,"
                         "lease_expires_at=excluded.lease_expires_at,payload_json=excluded.payload_json", (
                plan.plan_id, step.step_id, step.status, step.lease_token, step.lease_expires_at, step.model_dump_json(),
            ))

    def create(self, plan):
        with self.db.write() as conn:
            conn.execute("INSERT INTO action_plans VALUES (?,?,?,?,?)", (
                plan.plan_id, plan.scope_key, plan.revision, plan.status, plan.model_dump_json()))
            self._write_steps(conn, plan)
        return plan

    def save(self, plan, *, expected_revision, patch=None, conn=None):
        if conn is None:
            with self.db.write() as connection:
                return self.save(plan, expected_revision=expected_revision, patch=patch, conn=connection)
        old = self.get(plan.plan_id, conn=conn)
        if old is None or old.revision != expected_revision:
            raise ActionConflictError("plan_revision_conflict")
        current = {step.step_id: step for step in plan.steps}
        if any(step.status == "done" and current.get(step.step_id) != step for step in old.steps):
            raise ActionConflictError("done_step_is_immutable")
        updated = plan.model_copy(update={"revision": expected_revision + 1})
        cursor = conn.execute("UPDATE action_plans SET revision=?,status=?,payload_json=? WHERE plan_id=? AND revision=?",
                             (updated.revision, updated.status, updated.model_dump_json(), plan.plan_id, expected_revision))
        if cursor.rowcount != 1:
            raise ActionConflictError("plan_revision_conflict")
        self._write_steps(conn, updated)
        if patch:
            conn.execute("INSERT INTO action_patches VALUES (?,?,?,?,?)", (
                patch.patch_id, plan.plan_id, expected_revision, now_ms(), patch.model_dump_json()))
        return updated

    def change_step(self, plan_id, step_id, changes, *, token=None, conn=None):
        if conn is None:
            with self.db.write() as connection:
                return self.change_step(plan_id, step_id, changes, token=token, conn=connection)
        plan = self.get(plan_id, conn=conn)
        step = next(item for item in plan.steps if item.step_id == step_id)
        if token is not None and step.lease_token != token:
            raise ActionConflictError("step_lease_lost")
        updated_step = step.model_copy(update=changes)
        updated = plan.model_copy(update={"steps": tuple(updated_step if item.step_id == step_id else item for item in plan.steps)})
        return self.save(updated, expected_revision=plan.revision, conn=conn)

    def claim(self, plan_id, *, lease_ms=300000):
        with self.db.write() as conn:
            plan = self.get(plan_id, conn=conn)
            done = {step.step_id for step in plan.steps if step.status == "done"}
            step = next((step for step in plan.steps if step.status in {"todo", "ready"}
                         and set(step.depends_on) <= done and step.attempt < step.max_attempts), None)
            if step is None:
                return None
            updated = self.change_step(plan_id, step.step_id, {"status": "doing", "attempt": step.attempt + 1,
                "lease_token": uuid.uuid4().hex, "lease_expires_at": now_ms() + lease_ms, "error": None}, conn=conn)
            return next(item for item in updated.steps if item.step_id == step.step_id)

    def require_step(self, conn, context):
        if not context.plan_id:
            return
        plan = self.get(context.plan_id, conn=conn)
        step = next((step for step in plan.steps if step.step_id == context.step_id), None) if plan else None
        if (not step or plan.scope_key != context.scope_key or plan.actor_id != context.actor_id
            or plan.agent_id != context.agent_id or step.status != "doing" or step.lease_token != context.step_lease_token
            or not step.lease_expires_at or step.lease_expires_at <= now_ms()):
            raise ActionConflictError("step_lease_lost")
