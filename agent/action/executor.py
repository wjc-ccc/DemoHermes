"""唯一受控工具发送入口；超时不取消已经可能发生的副作用。"""
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from ..tools.base import ToolCallResult
from .guards.pre import PreGuard
from .guards.post import PostGuard
from .decision import GateDecision
from .ledger import ActionItem, digest, now_ms
from .storage.tool_store import ActionConflictError


class ToolExecutor:
    def __init__(self, catalog, ledger, config):
        self.catalog, self.ledger, self.config = catalog, ledger, config
        self.pre, self.post = PreGuard(catalog, ledger), PostGuard(catalog)
        self.worker_id = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._pool = ThreadPoolExecutor(max_workers=config.max_read_workers, thread_name_prefix="action-tool")
        self._active = set()
        self._lock = threading.Lock()

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

    def execute(self, request, context):
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
        except (ValueError, TypeError) as exc:
            decision = GateDecision(verdict="block", layer="syntax", reasons=(str(exc),))
        if decision.verdict != "accept":
            candidate = candidate.model_copy(update={"pre_decision": decision.model_dump(mode="json")})
            item = self.ledger.block(candidate, attempt, ",".join(decision.reasons))
            return ToolCallResult(ok=False, error=",".join(decision.reasons)), item, attempt, False
        candidate = candidate.model_copy(update={"pre_decision": decision.model_dump(mode="json")})
        try:
            item, owner = self.ledger.reserve(candidate, attempt,
                max_calls=context.toolset.max_calls.get(request.name, policy.max_calls_per_turn),
                max_attempts=1 if policy.idempotency == "non_idempotent" else policy.max_attempts,
                lease_ms=max(self.config.lease_ms, int(policy.timeout_s * 1000)))
        except ActionConflictError as exc:
            item = self.ledger.block(candidate, attempt, str(exc))
            return ToolCallResult(ok=False, error=str(exc)), item, attempt, False
        if not owner:
            if item.effect_state == "succeeded":
                return self.replay(item), item, attempt, True
            if item.phase == "prepared" and item.context.get("turn_id") == context.turn_id:
                return ToolCallResult(ok=True, result_text="修改已暂存，等待本轮提交。"), item, attempt, True
            return ToolCallResult(ok=False, error="unknown" if item.effect_state == "unknown" else "in_flight"), item, attempt, True
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
            raise TypeError("工具必须返回 ToolCallResult")
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
        if result.data and "memory_mutation" in result.data:
            # 成功动作的回放不得再次交出可提交的修改指令。
            result = result.model_copy(update={"data": {key: value for key, value in result.data.items() if key != "memory_mutation"},
                                              "result_text": "该卡片修改已经提交。"})
        return result

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
        self._pool.shutdown(wait=False, cancel_futures=True)

