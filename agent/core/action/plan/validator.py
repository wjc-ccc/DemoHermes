"""确定性 DAG 与补丁校验，外部文本没有重置步骤的权力。"""
from jsonschema import Draft202012Validator


class PlanValidator:
    def validate(self, plan):
        steps = {step.step_id: step for step in plan.steps}
        if len(steps) != len(plan.steps) or not steps:
            raise ValueError("duplicate_or_empty_steps")
        for step in plan.steps:
            if step.target_ref and not step.required_effects:
                raise ValueError("target_step_requires_effect_conditions")
            if len(set(step.depends_on)) != len(step.depends_on) or set(step.depends_on) - steps.keys():
                raise ValueError("invalid_dependency")
            if len({segment.segment_id for segment in step.chain}) != len(step.chain):
                raise ValueError("duplicate_segment")
            for segment in step.chain:
                Draft202012Validator.check_schema(segment.output_schema)
        visiting, visited = set(), set()
        def visit(identity):
            if identity in visiting:
                raise ValueError("dependency_cycle")
            if identity in visited:
                return
            visiting.add(identity)
            for dependency in steps[identity].depends_on:
                visit(dependency)
            visiting.remove(identity)
            visited.add(identity)
        for identity in steps:
            visit(identity)

    def affected(self, plan):
        affected = {step.step_id for step in plan.steps if step.status in {"failed", "blocked"}}
        while True:
            expanded = affected | {step.step_id for step in plan.steps if step.status != "done"
                                   and set(step.depends_on) & affected}
            if expanded == affected:
                return affected
            affected = expanded

    def apply(self, plan, patch, *, cap):
        if patch.base_revision != plan.revision:
            raise ValueError("plan_revision_conflict")
        existing = {step.step_id: step for step in plan.steps}
        affected = set(patch.affected_steps)
        if any(identity in existing and existing[identity].status == "done" for identity in affected):
            raise ValueError("done_step_is_immutable")
        if not affected or not affected <= self.affected(plan):
            raise ValueError("patch_outside_affected_subgraph")
        replacements = {step.step_id: step for step in patch.replace_steps}
        if len(replacements) != len(patch.replace_steps) or not replacements.keys() <= affected:
            raise ValueError("replacement_outside_affected_subgraph")
        for identity, replacement in replacements.items():
            original = existing[identity]
            if original.status == "done":
                raise ValueError("done_step_is_immutable")
            if original.status in {"unknown", "doing"}:
                raise ValueError("step_requires_reconciliation")
            if replacement.status != "todo" or replacement.artifact_refs or replacement.lease_token:
                raise ValueError("patch_cannot_supply_runtime_state")
            if not (set(original.depends_on) - affected) <= set(replacement.depends_on):
                raise ValueError("outside_dependencies_cannot_be_deleted")
            if (replacement.target_ref, replacement.target_version) != (original.target_ref, original.target_version):
                raise ValueError("frozen_target_changed")
            if replacement.toolset != original.toolset or not set(original.required_effects) <= set(replacement.required_effects):
                raise ValueError("step_authorization_cannot_be_expanded_or_removed")
            replacements[identity] = replacement.model_copy(update={"attempt": original.attempt})
        if len(patch.added_steps) > cap:
            raise ValueError("patch_cap_exceeded")
        added = {step.step_id: step for step in patch.added_steps}
        if len(added) != len(patch.added_steps) or added.keys() & existing.keys():
            raise ValueError("duplicate_added_step")
        if any(step.status != "todo" or step.attempt or step.artifact_refs or step.lease_token for step in added.values()):
            raise ValueError("patch_cannot_supply_runtime_state")
        candidate = plan.model_copy(update={"status": "pending", "steps": tuple(
            replacements.get(step.step_id, step) for step in plan.steps) + patch.added_steps})
        self.validate(candidate)
        # 保留原步骤 ID，因此范围外依赖它的节点仍然连接；新增节点不能悬空。
        connected = set(affected)
        allowed = affected | added.keys()
        while True:
            expanded = connected | {step.step_id for step in candidate.steps
                                   if step.step_id in allowed and set(step.depends_on) & connected}
            expanded |= {dep for step in candidate.steps if step.step_id in connected for dep in step.depends_on if dep in allowed}
            if expanded == connected:
                break
            connected = expanded
        if not added.keys() <= connected:
            raise ValueError("added_step_disconnected")
        return candidate
