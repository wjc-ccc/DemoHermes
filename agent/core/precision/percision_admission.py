"""条目级上下文准入：历史工具结果按规则、相关度和判官概率分为 keep / trim / defer。

只处理 P2 中尚未被 Anchor 覆盖的、足够长的、非报错的工具结果；用户消息、
assistant 消息、P0/P1 一律不碰。trim/defer 只改发给模型的投影，原文仍在
Session 与 tool_calls 表中，可用 memory_read(tool:<id>) 读回；读回即视为判错，
该条被钉住为全文。定案按 message_id 粘住，只升级不降级，避免反复改写前缀。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from agent.types.context import AdmissionVerdict
from agent.types.precision.judge import ContextJudge, ContextJudgeRequest
from agent.core.precision.errors import ContextJudgeError
from agent.types.precision.admission import AdmissionAction, AdmissionBand, AdmissionPolicy
from agent.core.precision.context_item import ContextItem, HistoryTurn

_SEVERITY: dict[str, int] = {"keep": 0, "trim": 1, "defer": 2}
# 没有可信判官答案时，每类输出日后仍被需要的先验。
_PRIOR: dict[str, float] = {
    "error": 0.30,
    "listing": 0.30,
    "log": 0.25,
    "content": 0.55,
    "data": 0.65,
    "other": 0.50,
}
_JUDGE_WEIGHT = 0.8
_PRIOR_WEIGHT = 0.6
_PATH_KEYS = ("path", "file_path", "filePath", "file", "filename")
_READ_MARKERS = ("read", "view", "cat", "open")
_WRITE_MARKERS = ("write", "edit", "patch", "replace", "create", "delete", "move", "rename")
_LISTING_MARKERS = ("search", "find", "grep", "glob", "list", "ls")
_LOG_MARKERS = ("bash", "shell", "exec", "run", "command", "terminal", "test")
_DATA_MARKERS = ("memory_", "fetch", "http", "web", "browse", "query")
_COMMON_TERMS = frozenset({
    "src", "lib", "test", "tests", "index", "main", "packages", "node_modules",
    "dist", "build", "true", "false", "null", "none", "grep", "find", "cat",
    "head", "tail", "echo", "bash", "read", "edit", "write", "with", "from",
    "this", "that", "the", "and", "json", "path", "file", "query",
})
_TERM_SPLIT = re.compile(r"[^\w.\-]+", re.UNICODE)
_MAX_TERMS = 16
_LIVE_TEXT_CHARS = 4000
_GOAL_USER_TURNS = 3
_GOAL_CHARS = 200
_CALL_CHARS = 160
_TRIM_MIN_SAVING = 200


@dataclass(frozen=True)
class _Call:
    """历史中的一次工具调用请求。"""

    seq: int
    tool_call_id: str
    name: str
    arguments: dict
    signature: str


@dataclass(frozen=True)
class _Candidate:
    """一条可被准入判定的历史工具结果。"""

    item: ContextItem
    tool_call_id: str
    call: _Call | None
    content: str
    age_turns: int
    calls_since: int
    relevance: float


@dataclass
class AdmissionPlan:
    """一次构建的准入结果；projected 仅在 active 下非空。"""

    projected: dict[str, ContextItem] = field(default_factory=dict)
    notes: dict[str, tuple[AdmissionAction, str]] = field(default_factory=dict)
    verdicts: dict[str, AdmissionVerdict] = field(default_factory=dict)
    updated: bool = False
    candidates: int = 0
    judged: int = 0
    judge_failures: int = 0
    judge_latency_ms: int = 0
    rule_count: int = 0
    trimmed: int = 0
    deferred: int = 0
    saved_tokens: int = 0
    missed: int = 0


def admit(
    p2_turns: Sequence[HistoryTurn],
    *,
    covered_count: int,
    live_items: Sequence[ContextItem],
    usage: float,
    policy: AdmissionPolicy,
    judge: ContextJudge | None,
    stored: Mapping[str, AdmissionVerdict],
    estimate: Callable[[dict], int],
) -> AdmissionPlan:
    """对未被 Anchor 覆盖的 P2 工具结果给出定案，并在 active 下产出投影。"""
    plan = AdmissionPlan(verdicts=dict(stored))
    plan.missed = sum(verdict.missed for verdict in stored.values())
    if policy.mode == "off":
        return plan

    history = sorted(
        [item for turn in p2_turns for item in turn] + list(live_items),
        key=lambda item: item.seq,
    )
    calls = _calls_of(history)
    call_by_id = {call.tool_call_id: call for call in calls if call.tool_call_id}
    user_seqs = [item.seq for item in history if item.message.get("role") == "user"]
    live_texts = [(item.seq, _searchable_text(item)) for item in history]
    goal = _goal_of(history)
    band = _band_for(usage, policy.bands)

    candidates = [
        candidate
        for turn in p2_turns[covered_count:]
        for item in turn
        if (candidate := _candidate_of(
            item, policy, call_by_id, calls, user_seqs, live_texts
        )) is not None
    ]
    plan.candidates = len(candidates)

    decided: dict[str, AdmissionVerdict] = {}
    pending: list[_Candidate] = []
    for candidate in candidates:
        message_id = candidate.item.message_id or ""
        previous = stored.get(message_id)
        if previous is not None:
            current = _escalate(previous, band)
            decided[message_id] = current
            if current != previous:
                plan.updated = True
            continue
        if band is None:
            continue
        stale = _stale_reason(candidate, calls)
        if stale is not None:
            decided[message_id] = _verdict(
                candidate, score=0.0, source="rule", reason=stale, band=band
            )
            plan.rule_count += 1
            plan.updated = True
            continue
        pending.append(candidate)

    if pending and band is not None:
        for verdict in _judge_pending(pending, goal, policy, judge, band, plan):
            decided[verdict.message_id] = verdict
            plan.updated = True

    for candidate in candidates:
        message_id = candidate.item.message_id or ""
        verdict = decided.get(message_id)
        if verdict is None:
            continue
        plan.verdicts[message_id] = verdict
        action = verdict.action
        if verdict.missed:
            plan.notes[candidate.item.name] = ("keep", f"读回误判后钉住：{verdict.reason}")
            continue
        projected = _project(candidate, verdict, policy, estimate)
        if projected is None:
            plan.notes[candidate.item.name] = ("keep", verdict.reason)
            continue
        plan.notes[candidate.item.name] = (action, verdict.reason)
        plan.saved_tokens += max(
            0, candidate.item.token_estimate - projected.token_estimate
        )
        if action == "trim":
            plan.trimmed += 1
        else:
            plan.deferred += 1
        if policy.mode == "active":
            plan.projected[candidate.item.name] = projected
    return plan


def mark_readback_miss(
    verdicts: Mapping[str, AdmissionVerdict], reference: str
) -> tuple[dict[str, AdmissionVerdict], AdmissionVerdict | None]:
    """模型读回了被移出/裁剪的结果：该定案判错，钉住为全文。"""
    prefix, _, tool_call_id = reference.partition(":")
    if prefix != "tool" or not tool_call_id:
        return dict(verdicts), None
    for key, verdict in verdicts.items():
        if verdict.tool_call_id != tool_call_id or verdict.missed:
            continue
        if verdict.action == "keep":
            return dict(verdicts), None
        pinned = verdict.model_copy(update={"action": "keep", "missed": True})
        return {**verdicts, key: pinned}, pinned
    return dict(verdicts), None


def _judge_pending(
    pending: Sequence[_Candidate],
    goal: str,
    policy: AdmissionPolicy,
    judge: ContextJudge | None,
    band: AdmissionBand,
    plan: AdmissionPlan,
) -> list[AdmissionVerdict]:
    """送判最长的若干条；判官缺席或失败时按工具类别先验兜底。"""
    if judge is None:
        return [_prior_verdict(candidate, band, None, "") for candidate in pending]

    ordered = sorted(pending, key=lambda candidate: -len(candidate.content))
    selected = ordered[: policy.max_judged]
    requests = [
        ContextJudgeRequest(
            item_id=candidate.item.message_id or candidate.item.name,
            goal=goal,
            call=_describe_call(candidate),
            result_head=candidate.content[: policy.judge_head_chars],
            result_chars=len(candidate.content),
            age_turns=candidate.age_turns,
            calls_since=candidate.calls_since,
        )
        for candidate in selected
    ]
    started = time.perf_counter()
    try:
        results = judge.judge(requests)
    except ContextJudgeError as exc:
        plan.judge_failures += 1
        plan.judge_latency_ms = int((time.perf_counter() - started) * 1000)
        return [
            _prior_verdict(candidate, band, None, f"判官不可用 {exc.code}")
            for candidate in selected
        ]
    except Exception as exc:  # 判官故障绝不阻断主链
        plan.judge_failures += 1
        plan.judge_latency_ms = int((time.perf_counter() - started) * 1000)
        return [
            _prior_verdict(candidate, band, None, f"判官异常 {type(exc).__name__}")
            for candidate in selected
        ]
    plan.judge_latency_ms = int((time.perf_counter() - started) * 1000)
    plan.judged = len(selected)

    by_id = {result.item_id: result for result in results}
    verdicts: list[AdmissionVerdict] = []
    for candidate, request in zip(selected, requests):
        result = by_id.get(request.item_id)
        if result is None or result.still_needed is None:
            verdicts.append(_prior_verdict(
                candidate, band, result.kind if result else None, "判官未给出需要度"
            ))
            continue
        score = (
            _JUDGE_WEIGHT * result.still_needed
            + (1 - _JUDGE_WEIGHT) * candidate.relevance
        )
        verdicts.append(_verdict(
            candidate,
            score=score,
            source="judge",
            reason=(
                f"判官 need={result.still_needed:.2f} "
                f"相关度={candidate.relevance:.2f}"
            ),
            band=band,
            kind=result.kind,
            still_needed=result.still_needed,
        ))
    return verdicts


def _prior_verdict(
    candidate: _Candidate,
    band: AdmissionBand,
    kind: str | None,
    note: str,
) -> AdmissionVerdict:
    """kind 先验与相关度的确定性兜底分。"""
    resolved = kind or _fallback_kind(candidate)
    score = (
        _PRIOR_WEIGHT * _PRIOR.get(resolved, _PRIOR["other"])
        + (1 - _PRIOR_WEIGHT) * candidate.relevance
    )
    reason = f"先验 {resolved} 相关度={candidate.relevance:.2f}"
    return _verdict(
        candidate,
        score=score,
        source="prior",
        reason=f"{reason}；{note}" if note else reason,
        band=band,
        kind=resolved,
    )


def _verdict(
    candidate: _Candidate,
    *,
    score: float,
    source: str,
    reason: str,
    band: AdmissionBand,
    kind: str | None = None,
    still_needed: float | None = None,
) -> AdmissionVerdict:
    """把分数按当前档位落成定案。"""
    clamped = min(1.0, max(0.0, score))
    return AdmissionVerdict(
        message_id=candidate.item.message_id or "",
        tool_call_id=candidate.tool_call_id,
        action=_action_for(clamped, band),
        score=round(clamped, 4),
        source=source,  # type: ignore[arg-type]
        reason=reason,
        kind=kind,
        still_needed=still_needed,
    )


def _escalate(
    previous: AdmissionVerdict, band: AdmissionBand | None
) -> AdmissionVerdict:
    """已定案条目不重判；用量进入更高档时只按原分数升级，读回钉住的不动。"""
    if previous.missed or band is None:
        return previous
    action = _action_for(previous.score, band)
    if _SEVERITY[action] <= _SEVERITY[previous.action]:
        return previous
    return previous.model_copy(update={"action": action})


def _action_for(score: float, band: AdmissionBand) -> AdmissionAction:
    """三区间：低分移出，中间裁剪，高分保留全文。"""
    if score <= band.defer_below:
        return "defer"
    if score < band.trim_below:
        return "trim"
    return "keep"


def _band_for(
    usage: float, bands: Sequence[AdmissionBand]
) -> AdmissionBand | None:
    """返回当前用量已跨过的最高一档。"""
    current: AdmissionBand | None = None
    for band in bands:
        if usage >= band.usage:
            current = band
    return current


def _candidate_of(
    item: ContextItem,
    policy: AdmissionPolicy,
    call_by_id: Mapping[str, _Call],
    calls: Sequence[_Call],
    user_seqs: Sequence[int],
    live_texts: Sequence[tuple[int, str]],
) -> _Candidate | None:
    """只有足够老、足够长、非报错、可追溯的工具结果才有资格被判。"""
    if item.message.get("role") != "tool" or item.is_error or not item.message_id:
        return None
    content = item.message.get("content")
    if not isinstance(content, str) or len(content) < policy.min_chars:
        return None
    age = sum(seq > item.seq for seq in user_seqs)
    if age < policy.min_age_turns:
        return None
    tool_call_id = str(item.message.get("tool_call_id") or "")
    call = call_by_id.get(tool_call_id)
    later_text = "\n".join(text for seq, text in live_texts if seq > item.seq)
    return _Candidate(
        item=item,
        tool_call_id=tool_call_id,
        call=call,
        content=content,
        age_turns=age,
        calls_since=sum(other.seq > item.seq for other in calls),
        relevance=_relevance(call, later_text),
    )


def _stale_reason(candidate: _Candidate, calls: Sequence[_Call]) -> str | None:
    """规则先行：之后同一调用重跑、同一文件重读或被修改，旧结果描述的世界已不存在。"""
    call = candidate.call
    if call is None:
        return None
    later = [other for other in calls if other.seq > candidate.item.seq]
    if any(
        other.name == call.name and other.signature == call.signature
        for other in later
    ):
        return "同一调用之后再次执行"
    path = _path_of(call.arguments)
    if path and _is_read(call.name):
        for other in later:
            if _path_of(other.arguments) != path:
                continue
            if _is_read(other.name):
                return "同一文件之后被再次读取"
            if _is_write(other.name):
                return "同一文件之后被修改"
    return None


def _project(
    candidate: _Candidate,
    verdict: AdmissionVerdict,
    policy: AdmissionPolicy,
    estimate: Callable[[dict], int],
) -> ContextItem | None:
    """渲染 trim/defer 后的工具消息；不省 token 时返回 None。"""
    if verdict.action == "keep":
        return None
    content = candidate.content
    hint = _recover_hint(candidate.tool_call_id, policy.readback)
    if verdict.action == "trim":
        edge = policy.edge_chars
        omitted = len(content) - 2 * edge
        if omitted < _TRIM_MIN_SAVING:
            return None
        text = (
            f"{content[:edge]}\n"
            f"[上下文准入] 中间 {omitted} 字已省略（{verdict.reason}）。{hint}\n"
            f"{content[-edge:]}"
        )
    else:
        text = (
            f"[上下文准入] {_describe_call(candidate)} 的 {len(content)} 字输出已移出上下文"
            f"（{verdict.reason}）。{hint}"
        )
    message = {**candidate.item.message, "content": text}
    projected = candidate.item.model_copy(update={
        "message": message,
        "raw_message": candidate.item.message,
        "token_estimate": max(0, int(estimate(message))),
    })
    if projected.token_estimate >= candidate.item.token_estimate:
        return None
    return projected


def _recover_hint(tool_call_id: str, readback: bool) -> str:
    """告诉主模型如何取回全文。"""
    if readback and tool_call_id:
        return f'需要原文时调用 memory_read(reference="tool:{tool_call_id}")。'
    return "需要原文时重新执行该调用。"


def _calls_of(history: Sequence[ContextItem]) -> list[_Call]:
    """从 assistant 的 tool_calls 中按顺序提取全部调用。"""
    calls: list[_Call] = []
    for item in history:
        for raw in item.message.get("tool_calls") or ():
            if not isinstance(raw, Mapping):
                continue
            function = raw.get("function") or {}
            arguments_raw = str(function.get("arguments") or "{}")
            arguments = _parse_arguments(arguments_raw)
            calls.append(_Call(
                seq=item.seq,
                tool_call_id=str(raw.get("id") or ""),
                name=str(function.get("name") or raw.get("name") or "tool"),
                arguments=arguments,
                signature=json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                if arguments
                else arguments_raw.strip(),
            ))
    return calls


def _parse_arguments(raw: str) -> dict:
    """解析工具参数 JSON；非法时视为空参数。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _searchable_text(item: ContextItem) -> str:
    """消息正文与调用参数，用于后续对话的词面相关度。"""
    parts = [str(item.message.get("content") or "")[:_LIVE_TEXT_CHARS]]
    for raw in item.message.get("tool_calls") or ():
        if isinstance(raw, Mapping):
            function = raw.get("function") or {}
            parts.append(str(function.get("arguments") or "")[:_LIVE_TEXT_CHARS])
    return "\n".join(parts).lower()


def _terms_of(call: _Call) -> list[str]:
    """文件名、标识符、检索词：后续对话回指这次调用时会用的词。"""
    terms: dict[str, None] = {}
    for value in call.arguments.values():
        if not isinstance(value, str):
            continue
        for word in _TERM_SPLIT.split(value[:400]):
            term = word.strip(".-").lower()
            if len(term) >= 3 and term not in _COMMON_TERMS and not term.isdigit():
                terms.setdefault(term)
    return list(terms)[:_MAX_TERMS]


def _relevance(call: _Call | None, later_text: str) -> float:
    """该调用的词项有多大比例仍出现在之后的对话里；无词项时取中性 0.5。"""
    if call is None:
        return 0.5
    terms = _terms_of(call)
    if not terms:
        return 0.5
    return sum(term in later_text for term in terms) / len(terms)


def _goal_of(history: Sequence[ContextItem]) -> str:
    """最近几条用户消息拼成判官的目标描述。"""
    users = [
        " ".join(str(item.message.get("content") or "").split())[:_GOAL_CHARS]
        for item in history
        if item.message.get("role") == "user"
    ]
    return " / ".join(text for text in users[-_GOAL_USER_TURNS:] if text)


def _describe_call(candidate: _Candidate) -> str:
    """单行调用描述，长参数截断。"""
    call = candidate.call
    if call is None:
        return "tool"
    flat = " ".join(call.signature.split())
    if len(flat) > _CALL_CHARS:
        flat = flat[:_CALL_CHARS] + "…"
    return f"{call.name} {flat}".strip()

### 关键词判定兜底
def _fallback_kind(candidate: _Candidate) -> str:
    """没有判官答案时按工具名推断输出类别。"""
    name = (candidate.call.name if candidate.call else "").lower()
    if _is_read(name):
        return "content"
    if any(marker in name for marker in _LISTING_MARKERS):
        return "listing"
    if any(marker in name for marker in _LOG_MARKERS):
        return "log"
    if any(marker in name for marker in _DATA_MARKERS):
        return "data"
    return "other"


def _path_of(arguments: Mapping[str, object]) -> str | None:
    """取文件类参数，统一去掉 ./ 前缀。"""
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value.removeprefix("./")
    return None


def _is_read(name: str) -> bool:
    """读文件类工具。"""
    lowered = name.lower()
    return any(marker in lowered for marker in _READ_MARKERS) and not _is_write(lowered)


def _is_write(name: str) -> bool:
    """改文件类工具。"""
    lowered = name.lower()
    return any(marker in lowered for marker in _WRITE_MARKERS)
