"""具体工具执行、效果落账、超时及发布；审批和分配由 ToolHandler 完成。"""
from __future__ import annotations
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from agent.types.tools import ToolCallResult, ToolDispatchItem
from agent.types.conversation import ToolCall
from agent.types.action.execution import ActionExecutionEvidence, digest
from agent.types.action.trace import ActionTrace


class ToolDispatch:
    def __init__(self, trace, *, registry=None, catalog=None, ledger=None, config=None):
        self.trace = trace
        self.registry = registry
        self.catalog, self.ledger, self.config = catalog, ledger, config
        self._active = set()
        self._lock = threading.Lock()
        self._pool = None
        if catalog is not None:
            from agent.action.guards.post import PostGuard
            self.post = PostGuard(catalog)
            self._pool = ThreadPoolExecutor(max_workers=config.max_read_workers, thread_name_prefix="action-tool")

    def dispatch(self, ticket: ToolDispatchItem) -> ToolCall:
        if ticket.mode == "return":
            result, item = ticket.result, ticket.action_item
        elif ticket.mode == "replay":
            result, item = self.revalidate_publication(ticket.action_item, ticket.context, ticket.policy)
        elif ticket.context is not None:
            result, item, _, _ = self._dispatch_action(ticket)
        else:
            result, item = self._dispatch_basic(ticket), None
        request = ticket.request
        record = ToolCall(
            tool_call_id=ticket.fact_id, session_id=ticket.invocation.session_id,
            trajectory_id=ticket.invocation.turn_id, tool_name=request.name,
            arguments=request.arguments, arguments_raw=request.arguments_raw,
            status="success" if result.ok else "error", created_at=ticket.started_at,
            finished_at=time.time(), durations_ms=int((time.time()-ticket.started_at)*1000),
            tool_call_result=result.data, result_text=result.result_text, error=result.error,
            pending_mutation=result.pending_mutation,
            readback_reference=result.readback_reference,
        )
        if item is not None:
            attempt = ticket.attempt
            record.provider_tool_call_id = request.id or attempt.tool_call_id
            record.action_evidence = ActionExecutionEvidence(
                ledger_entry_id=item.entry_id, attempt_id=attempt.attempt_id, status=item.status,
                effect_state=item.effect_state, publish_state=item.publish_state,
                deferred_commit=item.phase == "prepared", lease_token=item.lease_token,
                replayed=ticket.replayed, source_message_id=ticket.context.source_message_id,
            )
            record.action_trace = ActionTrace(
                trace_id=item.entry_id, ledger_entry_ids=(item.entry_id,), attempt_ids=(attempt.attempt_id,),
                decisions=tuple(value for value in (item.pre_decision,item.post_decision) if value is not None),
            )
        self.trace.tool_result(record, ticket.invocation)
        return record

    def _dispatch_basic(self, ticket):
        if self.registry is None:
            return ToolCallResult(ok=False, error="当前 Agent 未配置任何工具")
        tool = self.registry.get(ticket.request.name)
        if tool is None:
            return ToolCallResult(ok=False, error=f"工具不存在: {ticket.request.name}（可用: {self.registry.list_tools()}）")
        try:
            return tool.execute(ticket.effective_args or {}, context=ticket.invocation)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("工具执行异常 tool=%s", ticket.request.name)
            return ToolCallResult(ok=False, error=f"工具执行异常: {exc}")

    def _dispatch_action(self, ticket):
        request, context = ticket.request, ticket.context
        item, attempt, policy = ticket.action_item, ticket.attempt, ticket.policy
        effective, key = ticket.effective_args, item.idempotency_key
        entry = self.catalog.get(request.name)
        with self._lock:
            self._active.add(item.entry_id)
        future = self._pool.submit(entry["adapter"].invoke, effective, context, idempotency_key=key)
        try:
            raw = future.result(timeout=policy.timeout_s)
        except TimeoutError:
            uncertain = self.ledger.update(item, changes={"effect_state": "failed" if policy.effect == "read" else "unknown"},
                event_type="timeout", evidence={"attempt_id": attempt.attempt_id})
            if policy.effect == "read":
                self.ledger.release(uncertain)
            self.ledger.finish_attempt(attempt, entry_id=item.entry_id, state=uncertain.effect_state,
                                       reason="timeout", admitted=True)
            # 迟到结果仍记证据。写动作占用不会因为 timeout/lease 到期而直接释放。
            future.add_done_callback(lambda finished: self._late(finished, item.entry_id, item.lease_token,
                                                               attempt, context, effective, policy))
            return ToolCallResult(ok=False, error=uncertain.effect_state + ":timeout"), uncertain, attempt, False
        except Exception as exc:
            with self._lock:
                self._active.discard(item.entry_id)
            failed = self.ledger.update(item, changes={"effect_state": "failed" if policy.effect == "read" else "unknown"},
                                        event_type="exception", evidence={"error": str(exc)})
            if policy.effect == "read":
                self.ledger.release(failed)
            self.ledger.finish_attempt(attempt, entry_id=item.entry_id, state=failed.effect_state, reason=str(exc), admitted=True)
            return ToolCallResult(ok=False, error=failed.effect_state + ":" + str(exc)), failed, attempt, False
        with self._lock:
            self._active.discard(item.entry_id)
        result, item = self.settle(item, raw, context, effective, policy)
        self.ledger.finish_attempt(attempt, entry_id=item.entry_id, state=item.status, admitted=True)
        if result.ok and policy.effect == "read":
            for ref, observed in entry["adapter"].observations(raw, effective, context):
                self.ledger.observe(attempt, context, ref, observed)
        return result, item, attempt, False

    def settle(self, item, raw, context, effective, policy):
        if not isinstance(raw, ToolCallResult):
            item = self.ledger.update(item, changes={"effect_state": "failed" if policy.effect == "read" else "unknown"},
                                      event_type="invalid_tool_result")
            return ToolCallResult(ok=False, error="invalid_tool_result"), item
        deferred = policy.deferred_commit and raw.ok
        state = "started" if deferred else ("succeeded" if raw.ok else (
            "unknown" if policy.effect == "external" and not (raw.data or {}).get("confirmed_no_effect") else "failed"))
        # 先记录外部事实，再决定发布，POST 出错不能抹掉成功。
        item = self.ledger.update(item, changes={"effect_state": state, "phase": "prepared" if deferred else "finished",
            "raw_output": self._stored(raw, policy), "external_ref": (raw.data or {}).get("external_ref"),
            "lease_expires_at": item.lease_expires_at if deferred else None}, event_type="tool_returned")
        try:
            decision = self.post.check(item.tool_name, raw, context)
            published = ToolCallResult.model_validate(decision.effective_value) if decision.verdict == "accept" else None
        except Exception as exc:
            decision = GateDecision(verdict="block", layer="post", reasons=("post_guard_error",), detail=str(exc))
            published = None
        item = self.ledger.update(item, changes={"post_decision": decision.model_dump(mode="json"),
            "publish_state": "blocked" if published is None else ("accepted" if published == raw else "redacted"),
            "publishable_output": self._stored(published, policy) if published else None}, event_type="post_decision")
        if not deferred and item.effect_state not in {"started", "unknown"}:
            self.ledger.release(item)
        return published or ToolCallResult(ok=False, error="blocked_post:" + ",".join(decision.reasons)), item

    @staticmethod
    def _stored(result, policy):
        value = result.model_dump(mode="json")
        if policy.effect == "read" and len(json.dumps(value, ensure_ascii=False)) > 8192:
            return {"ok": result.ok, "result_text": "读取证据已记录", "data": {"output_hash": digest(value)}, "error": result.error}
        return value

    @staticmethod
    def replay(item):
        if item.publish_state == "blocked" or item.publishable_output is None:
            return ToolCallResult(ok=False, error="blocked_post")
        result = ToolCallResult.model_validate(item.publishable_output)
        if item.compensation_state == "succeeded":
            result = result.model_copy(update={"result_text": "原动作已成功执行，之后已完成补偿。"})
        if result.pending_mutation is not None:
            # 成功动作的回放不得再次交出可提交的修改指令。
            result = result.model_copy(update={"pending_mutation": None, "result_text": "该卡片修改已经提交。"})
        return result

    def revalidate_publication(self, item, context, policy):
        raw = ToolCallResult.model_validate(item.raw_output)
        try:
            decision = self.post.check(item.tool_name, raw, context)
            published = ToolCallResult.model_validate(decision.effective_value) if decision.verdict == "accept" else None
        except Exception as exc:
            decision = GateDecision(verdict="block", layer="post", reasons=("post_guard_error",), detail=str(exc))
            published = None
        item = self.ledger.update(item, changes={"post_decision": decision.model_dump(mode="json"),
            "publish_state": "blocked" if published is None else ("accepted" if published == raw else "redacted"),
            "publishable_output": self._stored(published, policy) if published else None}, event_type="publication_revalidated")
        return self.replay(item), item

    def _late(self, future, entry_id, token, attempt, context, effective, policy):
        try:
            raw = future.result()
            current = self.ledger.get(entry_id)
            if current and current.lease_token == token and current.effect_state == "unknown":
                self.settle(current, raw, context, effective, policy)
            elif current:
                self.ledger.update(current, changes={}, event_type="late_result", evidence={"attempt_id": attempt.attempt_id})
        except Exception:
            # 迟到异常无法证明副作用失败，保留 UNKNOWN。
            pass
        finally:
            with self._lock:
                self._active.discard(entry_id)

    def is_active(self, entry_id):
        with self._lock:
            return entry_id in self._active

    def stop(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

