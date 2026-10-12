"""Action 的账本、授权上下文、计划和恢复装配。"""
import json
from agent.types.action.config import ActionConfig
from agent.types.action.execution import ToolContext, digest
from agent.core.tool_handler import ToolHandler
from agent.core.tool_dispatch import ToolDispatch
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.event_bus import EventBus
from agent.core.perception.perception_trace import PerceptionTrace
from agent.types.action.policy import Toolset
from .storage.tool_store import SqliteToolLedgerStore
from .adapters.turn_commit import ActionTurnCommitHook
from .recovery import ActionRecovery
from .storage.plan_store import SqlitePlanStore
from .storage.artifact_store import SqliteArtifactStore
from .chain.gates import ChainGates
from .plan.validator import PlanValidator
from .plan.runner import PlanRunner


class ActionRuntime:
    def __init__(self, database, catalog, *, config=None, event_bus=None, trace=None):
        self.config = config or ActionConfig()
        self.catalog = catalog
        self.ledger = SqliteToolLedgerStore(database)
        self.trace = trace or AgentLoopTrace(event_bus if event_bus is not None else EventBus(), PerceptionTrace())
        default = Toolset(tools=catalog.names())
        self.toolsets = {item.name: item for item in self.config.toolsets or (default,)}
        if self.config.default_toolset not in self.toolsets:
            raise ValueError("默认工具集不存在")
        for toolset in self.toolsets.values():
            catalog.validate(toolset)
        if any(name not in self.toolsets for names in self.config.agent_toolsets.values() for name in names):
            raise ValueError("Agent 绑定了不存在的工具集")
        self.commit_hook = ActionTurnCommitHook(self.ledger)
        self.recovery = ActionRecovery(self)
        self.plans = SqlitePlanStore(database)
        self.artifacts = SqliteArtifactStore(database)
        self.gates = ChainGates(database, self.ledger, self.artifacts)
        self.plan_validator = PlanValidator()
        self.plan_runner = PlanRunner(self)
        self.dispatch = ToolDispatch(self.trace, catalog=catalog, ledger=self.ledger, config=self.config)
        self.handler = ToolHandler(self.dispatch, action=self, plan_guard=self.plans.require_step)

    def create_context(self, turn):
        memory_scope, assembly_policy = turn.memory_scope, turn.assembly_policy
        turn_id, session_key, agent_id = turn.turn_id, turn.session_key, turn.agent_id
        name = getattr(assembly_policy, "toolset", None) or self.config.default_toolset
        if name not in self.toolsets:
            raise ValueError("toolset_not_found")
        if self.config.agent_toolsets and name not in self.config.agent_toolsets.get(agent_id, ()):
            raise ValueError("agent_toolset_not_allowed")
        toolset = self.toolsets[name].model_copy(deep=True)
        actor = getattr(memory_scope, "user_id", None) or turn.author_id
        scope = getattr(memory_scope, "storage_key", None) or digest({"channel": turn.channel, "actor": actor,
                                                                      "session": turn.session_id if not actor else None})
        context = ToolContext(agent_id=agent_id, actor_id=actor or "", scope_key=scope,
            session_id=turn.session_id, session_key=session_key, turn_id=turn_id,
            source_message_id=turn.source_message_id, toolset=toolset, memory_scope=memory_scope,
            memory_identity=memory_scope.identity_payload() if memory_scope else {},
            contract_hashes={name: self.catalog.contract_hash(name) for name in toolset.tools},
            contracts={name: self.catalog.contract(name) for name in toolset.tools},
            plan_id=getattr(assembly_policy, "plan_id", None), step_id=getattr(assembly_policy, "step_id", None),
            segment_id=getattr(assembly_policy, "segment_id", None),
            step_lease_token=getattr(assembly_policy, "step_lease_token", None))
        if context.plan_id:
            with self.ledger.db.read() as conn:
                self.plans.require_step(conn, context)
            plan = self.plans.get(context.plan_id)
            step = next(step for step in plan.steps if step.step_id == context.step_id)
            if step.toolset != name:
                raise ValueError("outside_step_toolset")
            context = context.model_copy(update={"target_ref": step.target_ref, "target_version": step.target_version,
                "max_iterations": step.max_iterations, "contracts": {name: plan.contracts[name] for name in toolset.tools},
                "contract_hashes": {name: plan.contract_hashes[name] for name in toolset.tools},
                "source_message_id": plan.source_message_id or context.source_message_id,
                "plan_source_verified": bool(plan.source_message_id)})
        self.ledger.freeze_contracts(context, context.contracts)
        return context


    def stop(self):
        self.dispatch.stop()

    def recover(self, *, scope_key=None):
        return self.recovery.recover(scope_key=scope_key)

    def create_plan(self, plan, *, scope_key=None, actor_id=None, agent_id=None):
        if (scope_key is not None and plan.scope_key != scope_key) or (
            actor_id is not None and plan.actor_id != actor_id) or (agent_id is not None and plan.agent_id != agent_id):
            raise ValueError("plan_not_in_scope")
        self.plan_validator.validate(plan)
        if plan.source_message_id:
            with self.ledger.db.read() as conn:
                row = conn.execute("SELECT m.message_json FROM messages m JOIN sessions s ON s.session_id=m.session_id "
                    "WHERE m.message_id=? AND m.role='user' AND s.scope_key=?", (plan.source_message_id, plan.scope_key)).fetchone()
            metadata = json.loads(row[0]).get("metadata", {}) if row else {}
            if not row or metadata.get("source_type") in {"system_injected", "memory_recalled"}:
                raise ValueError("plan_source_not_authorized")
        if plan.status != "pending" or plan.revision != 1:
            raise ValueError("new_plan_cannot_supply_runtime_state")
        if any(step.status != "todo" or step.attempt or step.lease_token or step.artifact_refs for step in plan.steps):
            raise ValueError("new_plan_cannot_supply_runtime_state")
        for step in plan.steps:
            if step.toolset not in self.toolsets:
                raise ValueError("toolset_not_found")
            if not set(step.required_effects) <= set(self.toolsets[step.toolset].tools):
                raise ValueError("required_effect_not_in_toolset")
            if self.config.agent_toolsets and step.toolset not in self.config.agent_toolsets.get(plan.agent_id, ()):
                raise ValueError("agent_toolset_not_allowed")
        tools = {name for step in plan.steps for name in self.toolsets[step.toolset].tools}
        plan = plan.model_copy(update={"contracts": {name: self.catalog.contract(name) for name in tools},
                                      "contract_hashes": {name: self.catalog.contract_hash(name) for name in tools}})
        return self.plans.create(plan)

    def run_plan(self, plan_id, run_step, **identity):
        return self.plan_runner.run(plan_id, run_step, **identity)

    def apply_patch(self, plan_id, patch):
        with self.ledger.db.write() as conn:
            plan = self.plans.get(plan_id, conn=conn)
            if not plan:
                raise ValueError("plan_not_found")
            uncertain = conn.execute("SELECT 1 FROM action_items WHERE effect_state IN ('started','unknown') "
                "AND json_extract(payload_json,'$.context.plan_id')=? AND "
                "json_extract(payload_json,'$.context.step_id') IN (" + ",".join("?" for _ in patch.affected_steps) + ")",
                (plan_id, *patch.affected_steps)).fetchone() if patch.affected_steps else None
            if uncertain:
                raise ValueError("step_requires_reconciliation")
            candidate = self.plan_validator.apply(plan, patch, cap=self.config.patch_cap)
            original_toolsets = {step.toolset for step in plan.steps}
            for step in candidate.steps:
                if step.toolset not in self.toolsets or step.toolset not in original_toolsets:
                    raise ValueError("toolset_not_found")
                if not set(step.required_effects) <= set(self.toolsets[step.toolset].tools):
                    raise ValueError("required_effect_not_in_toolset")
            return self.plans.save(candidate, expected_revision=patch.base_revision, patch=patch, conn=conn)

    def approve(self, entry_id, *, actor_id, expires_at):
        """可信应用入口，不能从工具参数中获得审批身份；执行时重新 PRE。"""
        item = self.ledger.get(entry_id)
        refs = (item.pre_decision or {}).get("evidence_refs", ()) if item else ()
        binding = next((ref.split(":", 1)[1] for ref in refs if ref.startswith("approval_binding:")), None)
        if not binding:
            raise ValueError("没有等待审批的执行参数")
        self.ledger.approve(scope_key=item.scope_key, tool_name=item.tool_name, binding_hash=binding,
                            actor_id=actor_id, expires_at=expires_at)
