from jsonschema import Draft202012Validator
from ..decision import GateDecision
from ..ledger import digest
from ..policy import RISK_ORDER
from .memory import check_memory
from .skill import check_skill


def approval_binding(name, arguments, context, resource_version):
    return digest({"tool": name, "args": arguments, "scope": context.scope_key,
                   "actor": context.actor_id, "version": resource_version,
                   "contract": context.contract_hashes.get(name)})


class PreGuard:
    def __init__(self, catalog, ledger):
        self.catalog, self.ledger = catalog, ledger

    def check(self, name, arguments, context, *, replay=False, preliminary=False):
        def blocked(code):
            return GateDecision(verdict="block", layer="pre", reasons=(code,))
        if name not in context.toolset.tools or self.catalog.get(name) is None:
            return blocked("tool_not_in_toolset")
        policy = self.catalog.policy_for(name, arguments)
        if RISK_ORDER[policy.risk] > RISK_ORDER[context.toolset.max_risk]:
            return blocked("risk_not_allowed")
        if policy.permission in {"user", "owner"} and not context.actor_id:
            return blocked("permission_denied")
        if policy.permission == "admin" and context.role != "admin":
            return blocked("permission_denied")
        if any(str(key).startswith("_") for key in arguments):
            return blocked("internal_argument_forbidden")
        errors = list(Draft202012Validator(self.catalog.contract(name)["parameters"]).iter_errors(arguments))
        if errors:
            return blocked("invalid_arguments:" + ".".join(str(part) for part in errors[0].path))
        if self.catalog.contract_hash(name) != context.contract_hashes.get(name):
            return blocked("contract_drift")
        adapter = self.catalog.get(name)["adapter"]
        values = adapter.effective_args(arguments)
        if preliminary:
            return GateDecision(verdict="accept", layer="pre", effective_value=values)
        if not replay:
            reason = check_memory(values, context, adapter) if name == "memory_edit" else (
                check_skill(values) if name == "skill_manage" else None)
            if reason:
                return blocked(reason)
        resource_ref, actual_version = adapter.resource(values, context)
        if policy.precondition_field and not replay:
            expected = values.get(policy.precondition_field)
            if expected is None or str(expected) != actual_version or not self.ledger.has_observation(context, resource_ref, expected):
                return blocked("stale_precondition")
        if policy.requires_approval:
            binding = approval_binding(name, values, context, actual_version)
            if not self.ledger.approved(context, name, binding):
                return GateDecision(verdict="escalate", layer="pre", reasons=("approval_required",),
                                    evidence_refs=("approval_binding:" + binding,), effective_value=values)
        return GateDecision(verdict="accept", layer="pre", effective_value=values)

