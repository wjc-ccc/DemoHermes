from ..ledger import now_ms
from ..chain.chain import ChainRunner


class PlanRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.chain = ChainRunner(runtime)

    def run(self, plan_id, run_step, *, scope_key=None, actor_id=None, agent_id=None):
        runtime = self.runtime
        plan = runtime.plans.get(plan_id)
        if not plan or (scope_key is not None and plan.scope_key != scope_key) or (
            actor_id is not None and plan.actor_id != actor_id) or (agent_id is not None and plan.agent_id != agent_id):
            raise ValueError("plan_not_in_scope")
        runtime.recover(scope_key=plan.scope_key)
        plan = runtime.plans.get(plan_id)
        for step in plan.steps:
            if step.status in {"doing", "unknown"} and (not step.lease_expires_at or step.lease_expires_at <= now_ms()):
                effects = runtime.ledger.list(scope_key=plan.scope_key, plan_id=plan_id, step_id=step.step_id)
                uncertain = any(item.effect_state in {"started", "unknown"} or item.compensation_state in {"started", "unknown"}
                                for item in effects)
                runtime.plans.change_step(plan_id, step.step_id, {"status": "unknown" if uncertain else "ready",
                    "error": "reconciliation_pending" if uncertain else "resume_from_ledger", "lease_token": None,
                    "lease_expires_at": None})
        while True:
            step = runtime.plans.claim(plan_id)
            if step is None:
                break
            plan = runtime.plans.get(plan_id)
            try:
                artifacts, error = self.chain.run(plan, step, run_step)
            except Exception as exc:
                artifacts, error = {}, str(exc)
            effects = runtime.ledger.list(scope_key=plan.scope_key, plan_id=plan_id, step_id=step.step_id)
            uncertain = any(item.effect_state in {"started", "unknown"} or item.compensation_state in {"started", "unknown"}
                            for item in effects)
            failed_effect = any(item.effect != "read" and (item.effect_state == "failed" or item.publish_state == "blocked") for item in effects)
            missing_effect = any(not any(item.tool_name == name and item.effect_state == "succeeded"
                and item.publish_state in {"accepted", "redacted"} and (not step.target_ref or item.resource_ref == step.target_ref)
                and item.compensation_state in {"not_requested", "failed"}
                for item in effects) for name in step.required_effects)
            if missing_effect:
                error = error or "required_effect_missing"
            status = "unknown" if uncertain else ("failed" if error or failed_effect else "done")
            runtime.plans.change_step(plan_id, step.step_id, {"status": status, "artifact_refs": artifacts,
                "error": error or ("effect_not_confirmed" if uncertain or failed_effect else None),
                "lease_expires_at": None}, token=step.lease_token)
        plan = runtime.plans.get(plan_id)
        status = "done" if all(step.status == "done" for step in plan.steps) else (
            "blocked" if any(step.status in {"unknown", "doing", "blocked"} for step in plan.steps) else "failed")
        return runtime.plans.save(plan.model_copy(update={"status": status}), expected_revision=plan.revision)
