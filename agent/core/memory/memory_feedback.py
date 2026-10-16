"""轮次提交后消费持久反馈作业，只评判当时曝光版本与后续可观察事实。

数据库事务不跨越模型网络调用；重试与退避由现有作业队列持久化。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Mapping

from agent.types.memory.scope import MemoryScope
from agent.types.memory.feedback import (
    FEEDBACK_VERSION, feedback_handler_version, MemoryUsageRequestItem, MemoryUsageResultItem,
)
from .config import MemoryExperienceConfig, MemoryReviewConfig
from .document_text import estimate_markdown_tokens
from .memory_experience_judge import ExperienceJudgeError, card_view


logger = logging.getLogger(__name__)
class MemoryFeedback:
    def __init__(self, storage, judge, model_key: str, config=None, *,
                 review_config: MemoryReviewConfig | None = None, clock=time.time):
        self._storage, self._judge, self._model_key = storage, judge, model_key
        self._config = config or MemoryExperienceConfig()
        self._review = review_config or MemoryReviewConfig()
        self._clock = clock
        self._version = feedback_handler_version(model_key)
        self._worker_id = "memory-feedback:" + hashlib.sha256(model_key.encode()).hexdigest()[:12]

    def run_once(self) -> bool:
        """处理一个作业；失败时持久退避，达到上限后停止自动重试。"""
        if (self._judge is None or not self._config.enabled
                or not getattr(self._judge, "is_usable", True)):
            return False
        jobs = self._storage.jobs
        job = jobs.claim(job_type="memory.feedback", handler_version=self._version,
                         worker_id=self._worker_id, lease_ms=int(self._review.lease_seconds * 1000),
                         now=self._now_ms())
        if job is None:
            return False
        stopped = threading.Event()

        def renew():
            while not stopped.wait(max(0.01, self._review.lease_seconds / 3)):
                try:
                    jobs.renew(job, lease_ms=int(self._review.lease_seconds * 1000), now=self._now_ms())
                except Exception:
                    logger.warning("Memory feedback lease renewal failed", exc_info=True)
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            if job.input.get("model_key") != self._model_key:
                raise ValueError("feedback_model_mismatch")
            turn_id = job.input.get("turn_id") or job.turn_id
            if not turn_id or (job.turn_id is not None and job.turn_id != turn_id):
                raise ValueError("feedback_turn_mismatch")
            rows = self._storage.card_decisions.pending_usage(
                turn_id=turn_id, model_key=self._model_key, limit=3)
            requests, immediate = [], []
            for row in rows:
                request, reason = self._request(row, turn_id)
                if request is None:
                    immediate.append(MemoryUsageResultItem(str(row["id"]), reason=reason))
                elif not request.evidence:
                    immediate.append(MemoryUsageResultItem(str(row["id"]), reason="no_subsequent_evidence"))
                else:
                    requests.append(request)
            results = list(immediate)
            batch_size = max(1, min(int(self._config.judge_batch_size), 3))
            for start in range(0, len(requests), batch_size):
                results.extend(self._judge.judge_usage(requests[start:start + batch_size]))
            by_id = {str(row["id"]): row for row in rows}
            requested = {r.decision_id: r for r in requests}
            received = set()
            for result in results:
                decision_id, judgment = _result(result)
                if decision_id not in by_id or decision_id in received:
                    raise ExperienceJudgeError("invalid_decision_mapping")
                received.add(decision_id)
                request = requested.get(decision_id)
                judgment, refs = _validated_judgment(judgment, request)
                # 各条判定幂等；作业完成前崩溃后重放，不重复增加曝光贡献。
                self._storage.card_decisions.judge_usage(
                    decision_id, judgment, judge_key=getattr(self._judge, "id", FEEDBACK_VERSION),
                    evidence_refs=refs, expected_judged_at=by_id[decision_id].get("judged_at"))
            if received != set(by_id):
                raise ExperienceJudgeError("missing_decision_result")
            # 自动召回和手动读卡可能超过三张；当前批完成前先持久安排剩余批次。
            remaining = self._storage.card_decisions.pending_usage(
                turn_id=turn_id, model_key=self._model_key, limit=3)
            continuation_id = None
            if remaining:
                batch_key = hashlib.sha256("|".join(sorted(str(r["id"]) for r in remaining)).encode()).hexdigest()[:20]
                continuation = jobs.enqueue(
                    job_type="memory.feedback", handler_version=self._version,
                    input={"turn_id": turn_id, "model_key": self._model_key},
                    session_id=job.session_id, turn_id=turn_id,
                    dedupe_key=f"memory.feedback:{self._model_key}:{turn_id}:next:{batch_key}",
                )
                continuation_id = continuation.id
            jobs.complete(job, {"turn_id": turn_id, "decision_ids": sorted(received),
                                "continuation_job_id": continuation_id}, now=self._now_ms())
        except Exception as exc:
            # 网络故障保留待评状态，不能写成未知或负面评判。
            try:
                jobs.fail(job, f"memory_feedback:{type(exc).__name__}:{getattr(exc, 'code', 'failed')}",
                          available_at=self._now_ms() + int(self._review.backoff_seconds * 1000),
                          max_attempts=self._review.max_attempts, now=self._now_ms(),
                          details={"model_key": self._model_key,
                                   "code": getattr(exc, "code", type(exc).__name__)})
            except Exception:
                logger.warning("Memory feedback failure could not be persisted", exc_info=True)
        finally:
            stopped.set()
            heartbeat.join()
        return True

    def _now_ms(self):
        return int(self._clock() * 1000)

    def _request(self, row: Mapping[str, Any], turn_id: str):
        context = (row.get("result_json") or {}).get("context") or {}
        card = context.get("card")
        turn = self._storage.executions.get_record(turn_id)
        if not isinstance(card, Mapping) or turn is None:
            return None, "missing_exposure_snapshot"
        if row.get("turn_id") != turn_id or row.get("model_key") != self._model_key:
            return None, "invalid_exposure_identity"
        card_id = card.get("item_id") or card.get("card_id") or card.get("id")
        if str(card_id) != str(row.get("card_id")) or card.get("revision") != row.get("card_revision"):
            return None, "invalid_exposure_revision"
        try:
            scope = MemoryScope.model_validate(context.get("scope") or card.get("scope") or {})
        except (TypeError, ValueError):
            return None, "invalid_exposure_scope"
        if scope.storage_key != turn.scope.storage_key:
            return None, "invalid_exposure_scope"
        session = self._storage.sessions.get_session(turn.session_id)
        if session is None:
            return None, "missing_source_session"
        first_iteration = context.get("first_iteration")
        if isinstance(first_iteration, bool) or not isinstance(first_iteration, int) or first_iteration < 1:
            return None, "missing_exposure_boundary"
        cutoff = context.get("after_seq")
        trace = turn.perception_trace or {}
        build = next((b for b in trace.get("builds", ()) if b.get("iteration") == first_iteration), None)
        if cutoff is None and build is not None:
            seqs = [r.get("seq") for r in build.get("records", ())
                    if r.get("role") in {"user", "assistant", "tool"} and isinstance(r.get("seq"), int)]
            cutoff = max(seqs) if seqs else None
        if cutoff is not None and (isinstance(cutoff, bool) or not isinstance(cutoff, int)):
            return None, "invalid_exposure_boundary"
        exposed_at = context.get("exposed_at")
        if isinstance(exposed_at, (int, float)) and not isinstance(exposed_at, bool):
            exposed_at = float(exposed_at)
            if exposed_at > 100_000_000_000:  # 同时接受显式毫秒时间戳。
                exposed_at /= 1000
        else:
            exposed_at = None
        if cutoff is None and exposed_at is None:
            return None, "missing_exposure_boundary"

        message_ids = set(turn.assistant_message_ids)
        tool_ids = set(turn.tool_call_ids)
        by_provider = {}
        for message in session.messages:
            for call in message.metadata.get("tool_calls", ()):
                by_provider.setdefault(call.get("id"), []).append(message)

        def after(message):
            if cutoff is not None:
                return message.seq_id > cutoff
            return message.created_at >= exposed_at

        before, evidence = [], []
        for tool in session.tool_calls:
            if tool.tool_call_id not in tool_ids or tool.trajectory_id != turn_id or tool.tool_name.startswith("memory_"):
                continue
            matching = [m for m in by_provider.get(tool.provider_tool_call_id, ())
                        if m.message_id in message_ids and m.created_at <= tool.created_at]
            request_message = matching[-1] if matching else None
            is_after = after(request_message) if request_message else (
                exposed_at is not None and tool.created_at >= exposed_at)
            entry = {"reference": "tool:" + tool.tool_call_id, "kind": "action",
                     "name": tool.tool_name, "arguments": tool.arguments, "status": tool.status}
            if not is_after:
                before.append(entry)
                continue
            evidence.append(entry)
            outcome = tool.result_text or tool.error or ""
            # 单纯执行完成不能证明任务结果有帮助。
            if _substantive(outcome):
                evidence.append({"reference": entry["reference"], "kind": "outcome", "text": outcome,
                                 "status": tool.status})
        for message in session.messages:
            if (message.message_id not in message_ids or message.role != "assistant"
                    or message.status == "failed" or not after(message)
                    or message.metadata.get("origin") in {"gateway_error", "failed_turn"}):
                continue
            if message.content_text.strip() and not message.metadata.get("tool_calls"):
                evidence.append({"reference": "message:" + message.message_id, "kind": "answer",
                                 "text": message.content_text})
        user_id = context.get("source_user_message_id") or turn.user_message_id
        goal = next((m.content_text for m in session.messages
                     if m.message_id == user_id and m.message_id == turn.user_message_id and m.role == "user"), "")
        # 保留条件、工具参数及结果尾部，不对整段历史只截开头；片段都保留偏移。
        compact, compact_before, complete = _bounded_evidence(
            evidence, before, card, int(self._config.judge_input_tokens))
        if len(goal) > 1200:
            goal = json.dumps({"excerpts": _excerpts(goal, tuple(card.get("keywords") or ())),
                               "total_chars": len(goal)}, ensure_ascii=False)
            complete = False
        # 评价依据限于实际展示的正文，不能用卡片中未注入主模型的隐藏建议归因。
        visible_card = {"item_id": str(row["card_id"]), "revision": int(row["card_revision"]),
                        "scope": scope.model_dump(mode="json"),
                        "content": context.get("card_content") or card.get("content") or ""}
        return MemoryUsageRequestItem(str(row["id"]), str(row["card_id"]), int(row["card_revision"]),
            turn_id, self._model_key, visible_card, goal, tuple(compact), tuple(compact_before), complete), ""


def _result(value):
    if isinstance(value, MemoryUsageResultItem):
        return value.decision_id, value.as_dict()
    if is_dataclass(value):
        value = asdict(value)
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise ExperienceJudgeError("invalid_result")
    identity = value.get("decision_id") or value.get("item_id") or value.get("id")
    return str(identity), dict(value)


def _validated_judgment(value, request):
    adopted, effect = value.get("adopted", "unknown"), value.get("effect", "unknown")
    if adopted not in {"yes", "no", "unknown"} or effect not in {"helpful", "harmful", "neutral", "unknown"}:
        raise ExperienceJudgeError("invalid_labels")
    allowed = {e["reference"] for e in request.evidence} if request else set()
    refs = tuple(dict.fromkeys(value.get("evidence_refs") or ()))
    if any(not isinstance(ref, str) or ref not in allowed for ref in refs):
        raise ExperienceJudgeError("invalid_evidence_reference")
    actions = {e["reference"] for e in request.evidence if e.get("kind") in {"action", "answer"}} if request else set()
    outcomes = {e["reference"] for e in request.evidence if e.get("kind") == "outcome"} if request else set()
    if adopted == "yes" and not actions.intersection(refs):
        adopted = "unknown"
    if (adopted != "yes" or not outcomes.intersection(refs)
            or request is None or not request.evidence_complete):
        effect = "unknown"
    return {"adopted": adopted, "effect": effect,
            "reason": str(value.get("reason") or "observational_judgment")[:300]}, refs


def _substantive(text):
    return bool(text.strip()) and text.strip().casefold().rstrip(".!。！") not in {
        "success", "succeeded", "completed", "done", "ok", "true", "成功", "完成", "执行成功", "工具执行成功"}


def _bounded_evidence(evidence, before, card, token_budget):
    terms = tuple(str(v) for v in (card.get("keywords") or ()))
    complete = True
    compact = []
    for entry in evidence:
        current = dict(entry)
        for field in ("text", "arguments"):
            value = current.get(field)
            if value is None:
                continue
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            if len(text) > 1200:
                current[field] = {"excerpts": _excerpts(text, terms), "total_chars": len(text), "omitted": True}
                complete = False
        compact.append(current)
    # 为判官规则、问题和未截断的卡片语义字段预留预算。
    available = max(128, token_budget - 1200 - estimate_markdown_tokens(json.dumps(card_view(card), ensure_ascii=False)))
    selected = []
    # 将已执行动作与对应结果相邻放置，再补充最终回答。
    for entry in compact:
        if estimate_markdown_tokens(json.dumps([*selected, entry], ensure_ascii=False)) <= available:
            selected.append(entry)
        else:
            complete = False
    earlier = []
    for entry in before[-3:]:
        short = {"reference": entry["reference"], "kind": "action", "name": entry["name"],
                 "arguments": entry.get("arguments")}
        if estimate_markdown_tokens(json.dumps([*selected, *earlier, short], ensure_ascii=False)) <= available:
            earlier.append(short)
        else:
            complete = False
    return selected, earlier, complete


def _excerpts(text, terms):
    spans = [(0, min(180, len(text))), (max(0, len(text) - 300), len(text))]
    for term in terms[:8]:
        match = re.search(re.escape(term), text, re.IGNORECASE) if term else None
        if match:
            spans.append((max(0, match.start() - 80), min(len(text), match.end() + 160)))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [{"offset": start, "text": text[start:end]} for start, end in merged]
