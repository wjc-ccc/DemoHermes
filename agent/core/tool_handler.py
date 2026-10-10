"""工具执行前的授权、审批和分配；实际调用交给 ToolDispatch。"""
from __future__ import annotations
import json
import os
import time
import uuid
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Sequence
from agent.types.runtime import TurnContext
from agent.types.model import ToolCallRequest
from agent.types.conversation import ToolCall
from agent.types.tools import ToolCallResult, ToolDispatchItem, ToolInvocationContext, ToolTurnContext
from agent.types.action.execution import GateDecision, ActionItem, digest


class ToolHandler:
    def __init__(self, dispatch, *, registry=None, action=None, plan_guard=None):
        self.dispatch = dispatch
        self.registry = registry
        self.action = action
        self.trace = dispatch.trace
        self.plan_guard = plan_guard
        self.worker_id = f"{os.getpid()}:{uuid.uuid4().hex}"
        if action is not None:
            from agent.action.guards.pre import PreGuard
            self.catalog, self.ledger, self.config = action.catalog, action.ledger, action.config
            self.pre = PreGuard(self.catalog, self.ledger)

    def begin_turn(self, turn: TurnContext) -> ToolTurnContext:
        if self.action is not None:
            return ToolTurnContext.from_action(self.action.create_context(turn))
        return ToolTurnContext(
            turn=turn,
            invocation=ToolInvocationContext(
                session_id=turn.session_id, turn_id=turn.turn_id, session_key=turn.session_key,
                source_message_id=turn.source_message_id, memory_scope=turn.memory_scope,
            ),
            available_tools=tuple(self.registry.list_tools()) if self.registry is not None else None,
        )

    def get_schemas(self, context: ToolTurnContext) -> list[dict]:
        if context.action is not None:
            return self.catalog.schemas(context.action)
        return self.registry.get_schemas() if self.registry is not None else []

    def tool_names(self):
        return self.catalog.names() if self.action is not None else (self.registry.list_tools() if self.registry is not None else [])

    def handle_batch(self, requests: Sequence[ToolCallRequest], context: ToolTurnContext) -> Iterable[ToolCall]:
        if self.action is None:
            return self._basic_batch(requests, context)
        records, reads = [], []
        def flush():
            if reads:
                with ThreadPoolExecutor(max_workers=self.config.max_read_workers, thread_name_prefix="action-read") as pool:
                    records.extend(pool.map(lambda req: self._handle_one(req, context), reads))
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
                records.append(self._handle_one(request, context))
        flush()
        return tuple(records)

    def _basic_batch(self, requests, context):
        for request in requests:
            yield self._handle_one(request, context)

    def _handle_one(self, request, context):
        fact_id = uuid.uuid4().hex if self.action is not None else (request.id or uuid.uuid4().hex)
        started = time.time()
        self.trace.tool_start(request, context.invocation, fact_id)
        ticket = ToolDispatchItem(
            request=request, invocation=context.invocation, effective_args=request.arguments,
            fact_id=fact_id, started_at=started, context=context.action,
        )
        if context.action is not None:
            ticket = self._prepare(ticket)
        return self.dispatch.dispatch(ticket)

    def action_key(self, name, arguments, context, policy, call_id):
        if policy.effect == "read":
            identity = {"read_attempt": uuid.uuid4().hex}
        elif policy.idempotency == "keyed":
            identity = {field: arguments.get(field) for field in policy.key_fields}
        elif policy.idempotency == "idempotent":
            identity = arguments
        else:
            identity = {"call_id": call_id}
        return digest({"scope": context.scope_key, "tool": name, "operation": arguments.get("operation")
                       or arguments.get("action"), "identity": identity})

    def _prepare(self, ticket: ToolDispatchItem) -> ToolDispatchItem:
        from agent.action.storage.tool_store import ActionConflictError
        request, context = ticket.request, ticket.context
        attempt = self.ledger.begin_attempt(request, context)
        entry = self.catalog.get(request.name)
        effective = request.arguments
        candidate = ActionItem(entry_id=uuid.uuid4().hex, idempotency_key=attempt.attempt_id, tool_name=request.name,
            scope_key=context.scope_key,
            context=context.model_dump(mode="json"), contract_hash=context.contract_hashes.get(request.name, ""),
            input_hash=digest(effective), requested_args=request.arguments, effective_args=effective,
            lease_owner=self.worker_id)
        try:
            if request.arguments_raw:
                parsed = json.loads(request.arguments_raw)
                if not isinstance(parsed, dict) or parsed != request.arguments:
                    raise ValueError("invalid_argument_syntax")
            decision = self.pre.check(request.name, request.arguments, context, preliminary=True)
            if decision.verdict == "accept":
                effective = decision.effective_value
                policy = self.catalog.policy_for(request.name, effective)
                key = self.action_key(request.name, effective, context, policy, attempt.tool_call_id)
                resource_ref, version = entry["adapter"].resource(effective, context)
                candidate = candidate.model_copy(update={"idempotency_key": key,
                    "operation": effective.get("operation") or effective.get("action") or "",
                    "contract_version": policy.contract_version, "effect": policy.effect,
                    "effective_args": effective, "input_hash": digest(effective),
                    "resource_ref": resource_ref, "resource_version": version})
                existing = self.ledger.get_by_key(key) if policy.effect != "read" else None
                decision = self.pre.check(request.name, request.arguments, context,
                                          replay=bool(existing and existing.effect_state == "succeeded"))
                if decision.verdict == "accept" and decision.effective_value != effective:
                    decision = GateDecision(verdict="block", layer="pre", reasons=("effective_arguments_changed",))
        except (ValueError, TypeError) as exc:
            decision = GateDecision(verdict="block", layer="syntax", reasons=(str(exc),))
        if decision.verdict != "accept":
            candidate = candidate.model_copy(update={"pre_decision": decision.model_dump(mode="json")})
            item = self.ledger.block(candidate, attempt, ",".join(decision.reasons))
            return replace(ticket, mode="return", result=ToolCallResult(ok=False, error=",".join(decision.reasons)), action_item=item, attempt=attempt)
        candidate = candidate.model_copy(update={"pre_decision": decision.model_dump(mode="json")})
        try:
            item, owner = self.ledger.reserve(candidate, attempt,
                max_calls=context.toolset.max_calls.get(request.name, policy.max_calls_per_turn),
                max_attempts=1 if policy.idempotency == "non_idempotent" else policy.max_attempts,
                lease_ms=max(self.config.lease_ms, int(policy.timeout_s * 1000)),
                validate_context=(lambda conn: self.plan_guard(conn, context)) if self.plan_guard else None)
        except ActionConflictError as exc:
            item = self.ledger.block(candidate, attempt, str(exc))
            return replace(ticket, mode="return", result=ToolCallResult(ok=False, error=str(exc)), action_item=item, attempt=attempt)
        if not owner:
            if item.effect_state == "succeeded":
                return replace(ticket, mode="replay", action_item=item, attempt=attempt,
                               policy=policy, effective_args=effective, replayed=True)
            if item.phase == "prepared" and item.context.get("turn_id") == context.turn_id:
                return replace(ticket, mode="return", result=ToolCallResult(ok=True, result_text="修改已暂存，等待本轮提交。"), action_item=item, attempt=attempt, policy=policy, replayed=True)
            return replace(ticket, mode="return", result=ToolCallResult(ok=False, error="unknown" if item.effect_state == "unknown" else "in_flight"), action_item=item, attempt=attempt, policy=policy, replayed=True)
        return replace(ticket, action_item=item, attempt=attempt, policy=policy, effective_args=effective)
