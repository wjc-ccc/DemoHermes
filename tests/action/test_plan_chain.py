from agent.types.runtime import RuntimeConfig
import pytest
from agent.types.action.chain import ChainSegment, Artifact
from agent.types.action.execution import GateDecision
from agent.types.action.plan import Plan, PlanStep, PlanPatch
from agent.action.plan.validator import PlanValidator
from agent.types.channel import SessionSource
from agent.core.memory.memory_scope import build_memory_scope
from agent.runtime import build_runtime
from agent.types.model import ModelResponse


def test_patch_preserves_done_outside_dependencies_and_cap():
    validator = PlanValidator()
    plan = Plan(scope_key="u", actor_id="u", goal="发薪", steps=(
        PlanStep(step_id="s7", goal="E0007", status="done"),
        PlanStep(step_id="s12", goal="E0012", depends_on=("s7",), status="failed"),
        PlanStep(step_id="finish", goal="汇总", depends_on=("s12",))))
    with pytest.raises(ValueError, match="done_step_is_immutable"):
        validator.apply(plan, PlanPatch(base_revision=1, affected_steps=("s7", "s12"),
            replace_steps=(PlanStep(step_id="s7", goal="重复发薪"),), reason="整批重跑"), cap=2)
    with pytest.raises(ValueError, match="outside_dependencies"):
        validator.apply(plan, PlanPatch(base_revision=1, affected_steps=("s12",),
            replace_steps=(PlanStep(step_id="s12", goal="重试"),), reason="重试"), cap=2)
    patch = PlanPatch(base_revision=1, affected_steps=("s12",), replace_steps=(
        PlanStep(step_id="s12", goal="修复", depends_on=("s7",)),), reason="修复")
    candidate = validator.apply(plan, patch, cap=2)
    assert candidate.steps[0] == plan.steps[0] and candidate.steps[-1].depends_on == ("s12",)
    disconnected = patch.model_copy(update={"added_steps": (PlanStep(step_id="new", goal="无关", depends_on=("s7",)),)})
    with pytest.raises(ValueError, match="disconnected"):
        validator.apply(plan, disconnected, cap=2)
    with pytest.raises(ValueError, match="cap_exceeded"):
        validator.apply(plan, disconnected, cap=0)


class ChainModel:
    supports_images = False
    def __init__(self):
        self.calls = []
        self.bad_second = True
    def chat(self, messages, tools=None):
        prompt = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        if not prompt.startswith(("first", "second", "publish")):
            return ModelResponse(text="{}")
        which = "first" if prompt.startswith("first") else "second"
        self.calls.append(which)
        return ModelResponse(text='{"total":99}' if which == "second" and self.bad_second else '{"total":100}')


def test_rejected_artifact_stops_chain_and_resume_skips_accepted_segment(tmp_path):
    model = ChainModel()
    class Models:
        def get(self, *args):
            return model
    runtime = build_runtime(RuntimeConfig(use_action=True, use_memory=True, data_root=tmp_path), model_registry=Models())
    source = SessionSource(user_id="u")
    scope = build_memory_scope(source)
    action = runtime.action
    action.gates.register("balanced", lambda artifact: GateDecision(
        verdict="accept" if artifact.body["total"] == 100 else "block", layer="semantics", reasons=("total_mismatch",)))
    schema = {"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"], "additionalProperties": False}
    step = PlanStep(step_id="calculate", goal="核算", chain=(
        ChainSegment(segment_id="first", prompt="first", output_schema=schema),
        ChainSegment(segment_id="second", prompt="second", output_schema=schema, semantic_gates=("balanced",)),
        ChainSegment(segment_id="publish", prompt="publish", output_schema=schema)))
    plan = action.create_plan(Plan(scope_key=scope.storage_key, actor_id="u", goal="核算", steps=(step,)))
    try:
        failed = runtime.gateway.run_plan(plan.plan_id, source)
        assert failed.steps[0].status == "failed" and model.calls == ["first", "second"]
        with action.ledger.db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM action_artifacts WHERE status='rejected'").fetchone()[0] == 1
        model.bad_second = False
        patched = action.apply_patch(plan.plan_id, PlanPatch(base_revision=failed.revision,
            affected_steps=("calculate",), replace_steps=(step,), reason="修复核算"))
        done = runtime.gateway.run_plan(plan.plan_id, source)
        assert done.status == "done" and model.calls.count("first") == 1
        assert model.calls == ["first", "second", "second", "second"]
        assert done.steps[0].attempt == 2
        with pytest.raises(ValueError, match="plan_revision_conflict"):
            action.apply_patch(plan.plan_id, PlanPatch(base_revision=patched.revision,
                affected_steps=("calculate",), replace_steps=(step,), reason="过期补丁"))
    finally:
        runtime.stop()


def test_artifact_hash_scope_and_sources_are_checked(bank_runtime):
    _, _, runtime = bank_runtime
    segment = ChainSegment(segment_id="result")
    artifact = runtime.artifacts.put(Artifact.create(body="内容", scope_key="u", plan_id="p", step_id="s",
        segment_id="result", input_hash="i", source_refs=("turn:missing",)))
    assert runtime.gates.check(artifact, segment).reasons == ("turn_not_committed",)
    with pytest.raises(ValueError, match="artifact_not_accepted"):
        runtime.artifacts.read_accepted(artifact.artifact_id, artifact.content_hash, "another-user")


def test_unknown_step_must_reconcile_before_patch(bank_runtime):
    _, _, runtime = bank_runtime
    plan = runtime.create_plan(Plan(scope_key="u", actor_id="u", goal="发薪", steps=(PlanStep(step_id="pay", goal="发薪"),)))
    runtime.plans.change_step(plan.plan_id, "pay", {"status": "unknown"})
    current = runtime.plans.get(plan.plan_id)
    with pytest.raises(ValueError):
        runtime.apply_patch(plan.plan_id, PlanPatch(base_revision=current.revision, affected_steps=("pay",),
            replace_steps=(PlanStep(step_id="pay", goal="重发"),), reason="超时后重跑"))
