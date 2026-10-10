"""链段交出工件，工件经闸门验收后才能进入下一段。"""
from agent.types.action.execution import digest

from agent.types.action.chain import Artifact, ChainSegment


class ChainRunner:
    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, plan, step, run_step):
        import json
        from agent.types.conversation import TurnResult
        from agent.types.action.policy import AssemblyPolicy
        from agent.types.action.execution import GateDecision
        runtime = self.runtime
        inputs = dict(step.input_artifacts)
        for dependency in step.depends_on:
            upstream = next(item for item in plan.steps if item.step_id == dependency)
            inputs.update(upstream.artifact_refs)
        incoming = [runtime.artifacts.read_accepted(identity, content_hash, plan.scope_key)
                    for identity, content_hash in inputs.items()]
        for artifact in incoming:
            upstream_plan = runtime.plans.get(artifact.plan_id)
            upstream_step = next((item for item in upstream_plan.steps if item.step_id == artifact.step_id), None) if upstream_plan else None
            if upstream_step is None:
                return {}, "artifact_plan_not_found"
            specs = upstream_step.chain or (ChainSegment(segment_id="result", prompt=upstream_step.goal),)
            spec = next((item for item in specs if item.segment_id == artifact.segment_id), None)
            if spec is None:
                return {}, "artifact_segment_not_found"
            decision = runtime.gates.check(artifact, spec)
            runtime.artifacts.decide(artifact, decision, runtime.gates.fingerprint(spec))
            if decision.verdict != "accept":
                return {}, ",".join(decision.reasons) or decision.verdict
        produced = {}
        segments = step.chain or (ChainSegment(segment_id="result", prompt=step.goal),)
        for segment in segments:
            input_hash = digest({"inputs": inputs, "segment": segment.model_dump(mode="json")})
            cached = runtime.artifacts.latest(plan.plan_id, step.step_id, segment.segment_id, input_hash)
            if cached and cached.status == "accepted":
                decision = runtime.gates.check(cached, segment)
                cached = runtime.artifacts.decide(cached, decision, runtime.gates.fingerprint(segment))
                if decision.verdict != "accept":
                    return produced, ",".join(decision.reasons) or decision.verdict
                artifact = cached
            else:
                artifact = None
                failure = ""
                for retry in range(runtime.config.segment_retries + 1):
                    policy = AssemblyPolicy(toolset=step.toolset, plan_id=plan.plan_id, step_id=step.step_id,
                                            segment_id=segment.segment_id, step_lease_token=step.lease_token)
                    prompt = segment.prompt or step.goal
                    if incoming:
                        prompt += "\n已验收的输入工件：\n" + json.dumps([
                            {"artifact_id": item.artifact_id, "hash": item.content_hash, "body": item.body} for item in incoming], ensure_ascii=False)
                    if failure:
                        prompt += "\n上一段验收未通过：" + failure
                    result = run_step(step, segment, prompt, policy)
                    if not isinstance(result, TurnResult):
                        metadata = result.metadata
                        result = TurnResult(final_text=result.text, turn_id=metadata.get("turn_id", ""),
                            session_id=metadata.get("session_id", ""), completed=not (metadata.get("error") or metadata.get("persistence_error")),
                            error=metadata.get("error") or metadata.get("persistence_error"))
                    if not result.completed:
                        return produced, result.error or "turn_not_completed"
                    try:
                        body = runtime.gates.parse(result.final_text or "", segment)
                        syntax_error = None
                    except ValueError:
                        body, syntax_error = result.final_text or "", "invalid_artifact_syntax"
                    artifact = runtime.artifacts.put(Artifact.create(body=body, scope_key=plan.scope_key,
                        plan_id=plan.plan_id, step_id=step.step_id, segment_id=segment.segment_id, input_hash=input_hash,
                        source_refs=("turn:" + result.turn_id, *tuple("artifact:" + item.artifact_id + ":" + item.content_hash for item in incoming))))
                    decision = GateDecision(verdict="block", layer="syntax", reasons=(syntax_error,)) if syntax_error else runtime.gates.check(artifact, segment)
                    artifact = runtime.artifacts.decide(artifact, decision, runtime.gates.fingerprint(segment))
                    if decision.verdict == "accept":
                        break
                    if decision.verdict == "repair" and decision.effective_value is not None:
                        repaired = runtime.artifacts.put(Artifact.create(body=decision.effective_value, scope_key=plan.scope_key,
                            plan_id=plan.plan_id, step_id=step.step_id, segment_id=segment.segment_id, input_hash=input_hash,
                            source_refs=artifact.source_refs))
                        repaired_decision = runtime.gates.check(repaired, segment)
                        artifact = runtime.artifacts.decide(repaired, repaired_decision, runtime.gates.fingerprint(segment))
                        if repaired_decision.verdict == "accept":
                            break
                        return produced, ",".join(repaired_decision.reasons) or repaired_decision.verdict
                    failure = ",".join(decision.reasons) or decision.verdict
                    effects = runtime.ledger.list(scope_key=plan.scope_key, plan_id=plan.plan_id, step_id=step.step_id)
                    risky = any(item.effect != "read" and item.effect_state != "not_started"
                                and item.context.get("segment_id") == segment.segment_id for item in effects)
                    if decision.verdict != "retry" or not decision.retryable or risky or retry == runtime.config.segment_retries:
                        return produced, failure
            produced[artifact.artifact_id] = artifact.content_hash
            inputs = {artifact.artifact_id: artifact.content_hash}
            incoming = [artifact]
        return produced, None
