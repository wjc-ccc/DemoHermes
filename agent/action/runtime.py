"""Loop 的小接口：begin_turn → get_schemas → execute_batch。"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from ..core.types import ToolCall
from ..core.event_bus import LoopEvent
from .config import ActionConfig
from .context import ToolContext
from .executor import ToolExecutor
from .ledger import digest
from .policy import Toolset
from .storage.tool_store import SqliteToolLedgerStore
from .adapters.turn_commit import ActionTurnCommitHook
from .recovery import ActionRecovery


class ActionRuntime:
    def __init__(self, database, catalog, *, config=None, event_bus=None):
        self.config = config or ActionConfig()
        self.catalog = catalog
        self.ledger = SqliteToolLedgerStore(database)
        self.events = event_bus
        default = Toolset(tools=catalog.names())
        self.toolsets = {item.name: item for item in self.config.toolsets or (default,)}
        if self.config.default_toolset not in self.toolsets:
            raise ValueError("默认工具集不存在")
        for toolset in self.toolsets.values():
            catalog.validate(toolset)
        self.executor = ToolExecutor(catalog, self.ledger, self.config)
        self.commit_hook = ActionTurnCommitHook(self.ledger)
        self.recovery = ActionRecovery(self)

    def begin_turn(self, session, user_message, *, turn_id, session_key="", memory_scope=None,
                   assembly_policy=None, agent_id="main"):
        name = getattr(assembly_policy, "toolset", None) or self.config.default_toolset
        if name not in self.toolsets:
            raise ValueError("toolset_not_found")
        toolset = self.toolsets[name]
        actor = getattr(memory_scope, "user_id", None) or session.author_id
        scope = getattr(memory_scope, "storage_key", None) or digest({"channel": session.channel, "actor": actor,
                                                                      "session": session.session_id if not actor else None})
        context = ToolContext(agent_id=agent_id, actor_id=actor or "", scope_key=scope,
            session_id=session.session_id, session_key=session_key, turn_id=turn_id,
            source_message_id=user_message.message_id, toolset=toolset, memory_scope=memory_scope,
            memory_identity=memory_scope.identity_payload() if memory_scope else {},
            contract_hashes={name: self.catalog.contract_hash(name) for name in toolset.tools},
            contracts={name: self.catalog.contract(name) for name in toolset.tools},
            plan_id=getattr(assembly_policy, "plan_id", None), step_id=getattr(assembly_policy, "step_id", None),
            segment_id=getattr(assembly_policy, "segment_id", None))
        self.ledger.freeze_contracts(context, {name: self.catalog.contract(name) for name in toolset.tools})
        return context

    def get_schemas(self, context):
        return self.catalog.schemas(context)

    def _execute(self, request, context):
        started = time.time()
        if self.events:
            self.events.publish(LoopEvent(type="tool_call_start", turn_id=context.turn_id, session_id=context.session_id,
                session_key=context.session_key, data={"tool_call_id": request.id, "name": request.name,
                                                      "arguments": request.arguments}))
        result, item, attempt, replayed = self.executor.execute(request, context)
        record = ToolCall(tool_call_id=request.id or attempt.tool_call_id, session_id=context.session_id,
            trajectory_id=context.turn_id, tool_name=request.name, arguments=request.arguments,
            arguments_raw=request.arguments_raw, status="success" if result.ok else "error",
            created_at=started, finished_at=time.time(), durations_ms=int((time.time() - started) * 1000),
            tool_call_result=result.data, result_text=result.result_text, error=result.error,
            metadata={"action": {"ledger_entry_id": item.entry_id, "attempt_id": attempt.attempt_id,
                "status": item.status, "effect_state": item.effect_state, "publish_state": item.publish_state,
                "deferred_commit": item.phase == "prepared", "lease_token": item.lease_token, "replayed": replayed}})
        if self.events:
            self.events.publish(LoopEvent(type="tool_call_result", turn_id=context.turn_id, session_id=context.session_id,
                session_key=context.session_key, data={"tool_call_id": record.tool_call_id, "name": request.name,
                    "ok": result.ok, "result_preview": result.result_text[:100], "error": result.error,
                    "duration_ms": record.durations_ms, "ledger_entry_id": item.entry_id, "action_status": item.status}))
        return record

    def execute_batch(self, requests, context):
        requests = tuple(requests)
        records = []
        reads = []
        def flush():
            if reads:
                with ThreadPoolExecutor(max_workers=self.config.max_read_workers, thread_name_prefix="action-read") as pool:
                    records.extend(pool.map(lambda req: self._execute(req, context), reads))
                reads.clear()
        for request in requests:
            entry = self.catalog.get(request.name)
            try:
                is_read = entry and self.catalog.policy_for(request.name, request.arguments).effect == "read"
            except TypeError:
                is_read = False
            if is_read:
                reads.append(request)
            else:
                flush()
                records.append(self._execute(request, context))
        flush()
        return tuple(records)

    def stop(self):
        self.executor.stop()

    def recover(self, *, scope_key=None):
        return self.recovery.recover(scope_key=scope_key)

    def approve(self, entry_id, *, actor_id, expires_at):
        """可信应用入口，不能从工具参数中获得审批身份；执行时重新 PRE。"""
        item = self.ledger.get(entry_id)
        refs = (item.pre_decision or {}).get("evidence_refs", ()) if item else ()
        binding = next((ref.split(":", 1)[1] for ref in refs if ref.startswith("approval_binding:")), None)
        if not binding:
            raise ValueError("没有等待审批的执行参数")
        self.ledger.approve(scope_key=item.scope_key, tool_name=item.tool_name, binding_hash=binding,
                            actor_id=actor_id, expires_at=expires_at)

