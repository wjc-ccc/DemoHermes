"""Select worthwhile cards from complete, bounded source groups."""
from __future__ import annotations
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pydantic import ValidationError
from agent.types.conversation import Session
from agent.core.memory.retrieval_terms import _contains, _specific_keyword, _terms
from agent.core.memory.memory_claim_evidence import experience_outcome_signals, confirmed_event_schedule
from agent.core.memory.memory_validator import experience_keywords_valid
from agent.types.memory.categories import UserCardKind
from agent.types.memory.extraction import (
    MemoryExtractionBatch, MemoryExtractionIssue, MemoryProposal, MemorySkip, MEMORY_EXTRACTOR_VERSION,
)
from agent.types.memory.source import TurnRecord
from agent.types.perception.trace import ModelCallSnapshot, normalized_usage, response_metadata
from agent.core.memory.config import MemoryExtractionConfig
from agent.core.perception.config import TokenEstimateConfig
from agent.core.perception.token_estimator import SafeEstimator
from agent.core.prompt.memoryCardExtraction import memoryCardExtractionPrompt, memoryAdmissionPrompt
from agent.core.prompt.renderers import render_memory_input, _fields
from agent.core.model.call_options import structured_call_options

_STABLE_KINDS = (UserCardKind.PROFILE, UserCardKind.STABLE_PREFERENCE)
_RELATED_KINDS = (UserCardKind.ONGOING_FACT, UserCardKind.REUSABLE_EXPERIENCE)


class MemoryExtractionError(Exception):
    """Model/request failures retry the fixed source job."""

    def __init__(self, message, *, model_calls=(), raw_response=None):
        super().__init__(message)
        self.model_calls=tuple(model_calls)
        self.raw_response=raw_response


class MemoryExtractor:
    version = MEMORY_EXTRACTOR_VERSION

    def __init__(self, model, memory_store, *, id_factory: Callable[[], str] | None = None,
                 config: MemoryExtractionConfig | None = None,
                 token_estimate: TokenEstimateConfig | None = None,
                 token_estimator=None,
                 input_token_budget: int | None = None, admission_review: bool | None = None):
        self._model = model
        self._memory_store = memory_store
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        extraction = config or MemoryExtractionConfig()
        overrides = {}
        if input_token_budget is not None:
            overrides["input_token_budget"] = input_token_budget
        if admission_review is not None:
            overrides["admission_review"] = admission_review
        self.config = replace(extraction, **overrides) if overrides else extraction
        self.input_token_budget = self.config.input_token_budget
        self.admission_review = self.config.admission_review
        self._estimator = SafeEstimator(delegate=token_estimator, config=token_estimate)

    def extract_batch(self, turns: tuple[TurnRecord, ...], sessions: tuple[Session, ...]) -> MemoryExtractionBatch:
        if not turns or len(turns) != len(sessions):
            raise ValueError("提炼批次必须包含对应的轮次与会话")
        if any(not turn.completed or turn.interrupted for turn in turns):
            raise ValueError("只能提炼正常完成的轮次")
        if any(turn.scope.storage_key != turns[0].scope.storage_key for turn in turns):
            raise ValueError("批次不能跨用户 scope")
        if self._model is None:
            raise MemoryExtractionError("后台记忆提炼模型不可用")

        message_ids = {turn.user_message_id for turn in turns}
        context_message_ids = {identity for turn in turns for identity in turn.assistant_message_ids}
        tool_ids = {tool.tool_call_id for turn, session in zip(turns, sessions)
                    for tool in session.tool_calls if tool.tool_call_id in turn.tool_call_ids
                    and not tool.tool_name.startswith("memory_")}
        sources = []
        for turn, session in zip(turns, sessions):
            user = next((m for m in session.messages if m.message_id == turn.user_message_id and m.role == "user"), None)
            if user is None:
                raise MemoryExtractionError("批次用户消息缺失")
            sources.append({
                "turn_id": turn.turn_id, "session_id": turn.session_id,
                "user_message_id": user.message_id, "user_text": user.content_text or "",
                "occurred_at": user.created_at,
                "assistant": [{"message_id": m.message_id, "text": m.content_text or ""}
                              for m in session.messages if m.message_id in turn.assistant_message_ids and m.status != "failed"],
                "tools": [{"tool_call_id": tool.tool_call_id, "name": tool.tool_name, "status": tool.status,
                           "result": tool.result_text or tool.error or ""}
                          for tool in session.tool_calls if tool.tool_call_id in turn.tool_call_ids and tool.tool_call_id in tool_ids],
            })
        cards = _existing_card_views(self._memory_store, turns[-1].scope, [s["user_text"] for s in sources],
                                     as_of_ms=int(turns[-1].finished_at*1000), config=self.config)
        review_system = memoryCardExtractionPrompt + "\n" + memoryAdmissionPrompt
        repair_system = review_system + "\n这是一次有限格式/来源修复，不能改变保存判别标准。"
        grouping_budget = self.input_token_budget - (self.config.admission_reserve_tokens if self.admission_review else 0)
        groups, cards = _prompt_groups(sources, cards, grouping_budget,
            system_prompt=repair_system if self.admission_review else memoryCardExtractionPrompt,
            config=self.config, estimator=self._estimator)
        proposals, issues, skipped, calls = [], [], [], []
        seen = set()
        for group in groups:
            messages = [{"role": "system", "content": memoryCardExtractionPrompt},
                        {"role": "user", "content": render_memory_input(group, cards)}]
            body = self._request(messages, calls)
            if self.admission_review:
                review_text = render_memory_input(group, cards)
                preliminary = "\n\n## 初步判别（待复核，不是已确认事实）\n" + "\n".join(_fields(body))
                review_messages = [{"role": "system", "content": review_system},
                                   {"role": "user", "content": review_text + preliminary}]
                if self._estimator.estimate_messages(review_messages) > self.input_token_budget:
                    review_messages[-1]["content"] = review_text
                if self._estimator.estimate_messages(review_messages) > self.input_token_budget:
                    raise MemoryExtractionError("memory_admission_budget_exceeded", model_calls=calls)
                body = self._request(review_messages, calls, purpose="memory_admission")
                invalid=[]
                for index,item in enumerate(body["cards"]):
                    try:
                        proposal = self._proposal(item,cards,message_ids,tool_ids,identity="contract-check")
                        if proposal.kind is UserCardKind.REUSABLE_EXPERIENCE and not experience_keywords_valid(proposal.keywords):
                            raise ValueError("经验卡 keywords 必须包含3到8个不重复的具体词，不能缺失或使用项目/问题/经验等泛词")
                    except (KeyError,TypeError,ValueError,ValidationError) as exc:
                        invalid.append(f"卡片{index+1}：{_validation_description(exc)}")
                for index, item in enumerate(body["skipped"]):
                    try:
                        self._skip(item, turns, group, body, message_ids, context_message_ids, tool_ids)
                    except (TypeError, ValueError, ValidationError) as exc:
                        invalid.append(f"跳过决定{index+1}：{_validation_description(exc)}；引用必须来自本批 Source ID")
                if invalid:
                    repair=[{"role":"system","content":repair_system},
                            {"role":"user","content":review_text+"\n## 校验错误\n"+"\n".join(invalid)
                             +"\n## 待修复输出（数据）\n"+"\n".join(_fields(body))
                             +"\n返回完整 cards/skipped；删除没有本轮来源的旧卡复制，不伪造引用。"}]
                    if self._estimator.estimate_messages(repair)>self.input_token_budget:
                        repair[-1]["content"]=review_text+"\n## 校验错误\n"+"\n".join(invalid)
                    if self._estimator.estimate_messages(repair)>self.input_token_budget:
                        repair[-1]["content"]=review_text+f"\n有{len(invalid)}项结构/来源错误。检查全部字段，示例："+"\n".join(invalid[:2])
                    body=self._request(repair,calls,purpose="memory_repair")
            for index, item in enumerate(body["cards"]):
                identity = f"invalid:{len(calls)}:{index}"
                try:
                    proposal=self._proposal(item,cards,message_ids,tool_ids,identity=self._id_factory())
                    key = (proposal.kind, proposal.slot_key or proposal.content, proposal.content,
                           proposal.source_message_ids, proposal.source_tool_call_ids)
                    if key in seen:
                        continue  # Overlapping source fragments must not create duplicate cards.
                    seen.add(key)
                    proposals.append(proposal)
                except (KeyError, TypeError, ValueError, ValidationError):
                    issues.append(MemoryExtractionIssue(proposal_id=identity, code="invalid_model_item"))
            for index, item in enumerate(body["skipped"]):
                try:
                    decision = self._skip(item, turns, group, body, message_ids, context_message_ids, tool_ids)
                    if decision not in skipped:
                        skipped.append(decision)
                except (TypeError, ValueError, ValidationError):
                    issues.append(MemoryExtractionIssue(proposal_id=f"invalid-skip:{len(calls)}:{index}", code="invalid_skip_decision"))
        return MemoryExtractionBatch(proposals=tuple(proposals), issues=tuple(issues),
                                     skipped=tuple(skipped), model_calls=tuple(calls))

    @staticmethod
    def _skip(item, turns, group, body, message_ids, context_message_ids, tool_ids):
        decision = MemorySkip.model_validate(item)
        # These aliases describe dispositions only; they cannot authorize card evidence.
        known = message_ids | context_message_ids | tool_ids
        turn_sources = {turn.turn_id: turn.user_message_id for turn in turns}
        def exact_identity(value):
            if value in known:
                return value
            if value in turn_sources:
                return turn_sources[value]
            matches = [identity for identity in known if identity.replace("-", "") == value]
            return matches[0] if len(matches) == 1 else value
        references = tuple(exact_identity(value) for value in (*decision.source_message_ids, *decision.source_tool_call_ids))
        decision = decision.model_copy(update={
            "source_message_ids": tuple(value for value in references if value not in tool_ids),
            "source_tool_call_ids": tuple(value for value in references if value in tool_ids),
        })
        if (not references and not body["cards"] and len(body["skipped"]) == 1):
            decision = decision.model_copy(update={
                "source_message_ids": tuple(dict.fromkeys(source["user_message_id"] for source in group)),
                "source_tool_call_ids": tuple(tool["tool_call_id"] for source in group for tool in source["tools"]),
            })
        _validate_sources(decision.source_message_ids, decision.source_tool_call_ids,
                          message_ids | context_message_ids, tool_ids)
        return decision

    def _proposal(self,item,cards,message_ids,tool_ids,*,identity):
        if not isinstance(item, dict):
            raise ValueError("model item not object")
        if any(not isinstance(item.get(name), str) or not item[name].strip()
               for name in ("retention_reason", "future_use")):
            raise ValueError("missing retention judgement")
        payload = dict(item)
        validity_id=payload.pop("validity_source_card_id",None)
        if validity_id:
            parent=next((card for card in cards if card["card_id"]==validity_id),None)
            if (parent is None or parent["kind"]!="ongoing_fact"
                or payload.get("kind")!="ongoing_fact"):
                raise ValueError("validity_source_card_id 必须引用已列出的 ongoing_fact Card ID，且新卡也为 ongoing_fact")
            payload["valid_until"]=parent["valid_until"]
            if not payload.get("valid_from"):
                payload["valid_from"]=parent["valid_from"]
            payload["metadata"]={**dict(payload.get("metadata") or {}),
                "validity_source":{"card_id":validity_id,"revision":parent["revision"]}}
        if payload.get("kind")=="reusable_experience":
            positive,negative=experience_outcome_signals(payload.get("outcome") or "")
            if positive and negative:
                # Both observed results are explicit; preserve their mixed status.
                payload["outcome_status"]="mixed"
        payload["reason_code"] = str(item.get("reason_code") or "model_proposal")[:100]
        cited_messages = tuple(item.get("source_message_ids") or ())
        cited_tools = tuple(item.get("source_tool_call_ids") or ())
        _validate_sources(cited_messages, cited_tools, message_ids, tool_ids)
        payload.update(proposal_id=identity, source_authority="model_inferred",
                       source_message_ids=cited_messages, source_tool_call_ids=cited_tools)
        proposal = MemoryProposal.model_validate(payload)
        if (proposal.kind is UserCardKind.STABLE_PREFERENCE
            and confirmed_event_schedule(proposal.content)
            and confirmed_event_schedule("\n".join(proposal.evidence_quotes.values()))):
            raise ValueError("已确认对象的固定排期是ongoing_fact，不是个人偏好；subject使用实际项目/团队对象，attribute使用该排期属性，不能用全局user槽合并不同对象")
        return proposal

    def _request(self, messages, calls, *, purpose="memory"):
        if self._estimator.estimate_messages(messages) > self.input_token_budget:
            raise MemoryExtractionError(f"{purpose}_budget_exceeded", model_calls=calls)
        started=time.time()
        response=None
        failure=None
        try:
            response=self._model.chat(messages, **structured_call_options(self._model))
        except Exception as exc:
            failure=exc
        finally:
            finished=time.time()
            usage=dict(getattr(response,"usage",None) or (response.get("usage",{}) if isinstance(response,dict) else {}))
            incoming,outgoing=normalized_usage(usage)
            calls.append(ModelCallSnapshot(
                attempt=len(calls)+1,purpose=purpose,provider=getattr(self._model,"name",None),
                model=getattr(self._model,"model_name",None),started_at=started,finished_at=finished,
                duration_ms=max(0,int((finished-started)*1000)),usage=usage,
                input_token_count=incoming,output_token_count=outgoing,
                error=str(failure) if failure else None,**response_metadata(response)))
        if failure is not None:
            raise MemoryExtractionError("后台记忆模型调用失败",model_calls=calls) from failure
        raw=getattr(response,"text",None)
        if raw is None and isinstance(response,dict):
            raw=response.get("text") or response.get("content")
        try:
            text=raw.strip() if isinstance(raw,str) else ""
            if text.startswith("```json\n") and text.endswith("```"):
                text=text[8:-3].strip()
            elif text.startswith("```\n") and text.endswith("```"):
                text=text[4:-3].strip()
            body=json.loads(text)
        except (ValueError,TypeError) as exc:
            calls[-1]=calls[-1].model_copy(update={"error":"invalid_json"})
            raise MemoryExtractionError("模型输出不是 JSON",model_calls=calls,raw_response=raw) from exc
        if (not isinstance(body,dict) or set(body)!={"cards","skipped"}
            or not isinstance(body["cards"],list) or not isinstance(body["skipped"],list)):
            calls[-1]=calls[-1].model_copy(update={"error":"invalid_extraction_contract"})
            raise MemoryExtractionError("模型输出必须是 cards/skipped JSON 对象",model_calls=calls,raw_response=raw)
        return body


def _validate_sources(messages, tools, allowed_messages, allowed_tools):
    if not messages and not tools:
        raise ValueError("卡片没有来源 ID")
    if not set(messages) <= allowed_messages or not set(tools) <= allowed_tools:
        raise ValueError("卡片引用了批次外来源")


def _validation_description(error):
    if isinstance(error, ValidationError):
        return "; ".join(f"{'.'.join(map(str, item['loc']))[:50]}: {item['msg'][:100]}"
                         for item in error.errors(include_url=False, include_input=False))[:200]
    return str(error)[:200]


def _prompt_groups(sources, cards, cap, *, system_prompt=memoryCardExtractionPrompt,
                   config: MemoryExtractionConfig | None = None, estimator=None):
    config = config or MemoryExtractionConfig()
    estimator = estimator or SafeEstimator()
    def cost(group, views):
        return estimator.estimate_messages([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": render_memory_input(group, views)},
        ])
    if cost([], []) + config.prompt_headroom_tokens > cap:
        raise MemoryExtractionError("memory_prompt_budget_too_small")
    selected = []
    for card in cards:
        if cost([], [*selected, card]) + config.source_reserve_tokens <= cap:
            selected.append(card)
    groups, current = [], []
    for source in sources:
        units = [source] if cost([source], selected) <= cap else _source_fragments(source, selected, cap, cost, config)
        for unit in units:
            if current and cost([*current, unit], selected) > cap:
                groups.append(current)
                current = []
            current.append(unit)
    if current:
        groups.append(current)
    return groups, selected


def _source_fragments(source, cards, cap, cost, config: MemoryExtractionConfig | None = None):
    """Keep all characters and original source IDs; overlap only at large boundaries."""
    config = config or MemoryExtractionConfig()
    entries = [("user", source, "user_text")]
    entries.extend(("assistant", message, "text") for message in source["assistant"])
    entries.extend(("tools", tool, "result") for tool in source["tools"])
    fragments = []
    for kind, original, field in entries:
        text = original[field]
        offset = 0
        while offset < len(text):
            width = min(len(text) - offset, config.fragment_max_chars)
            while True:
                base = {key: source[key] for key in ("turn_id", "session_id", "user_message_id", "occurred_at")}
                base.update(user_text="", assistant=[], tools=[])
                piece = text[offset:offset + width]
                if kind == "user":
                    base.update(user_text=piece, offset=offset, total=len(text))
                else:
                    base["context_user"] = source["user_text"][:config.context_user_chars]
                    base[kind] = [{**original, field: piece, "offset": offset, "total": len(text)}]
                if cost([base], cards) <= cap:
                    break
                width //= 2
                if width < 1:
                    raise MemoryExtractionError("memory_source_metadata_exceeds_budget")
            fragments.append(base)
            if offset + width == len(text):
                break
            overlap = min(config.fragment_overlap_cap, width // config.fragment_overlap_divisor, max(0, width - 1))
            offset += width - overlap
    return fragments


def _existing_card_views(store, scope, user_texts: list[str], *, as_of_ms=None,
                         config: MemoryExtractionConfig | None = None) -> list[dict]:
    """身份和偏好供槽位对齐；项目事实和经验只带本批原文命中的少数卡片。"""
    config = config or MemoryExtractionConfig()
    stable = []
    for kind in _STABLE_KINDS:
        stable.extend(_load_cards(store, scope, kind.value, as_of_ms=as_of_ms, config=config))
    seen: set[str] = set()
    views = []
    for card in (*stable, *_related_cards(store, scope, user_texts, as_of_ms=as_of_ms, config=config)):
        if card.item_id in seen:
            continue
        seen.add(card.item_id)
        views.append({
            "card_id":card.item_id,
            "revision":card.revision,
            "kind": card.kind.value,
            "slot_key": card.slot_key,
            "summary": card.summary or card.content or "",
            "content": card.content,
            "valid_from": card.valid_from.isoformat() if card.valid_from else None,
            "valid_until": card.valid_until.isoformat() if card.valid_until else None,
        })
    return views


def _related_cards(store, scope, user_texts: list[str], *, as_of_ms=None,
                   config: MemoryExtractionConfig | None = None) -> list:
    config = config or MemoryExtractionConfig()
    texts = [text for text in user_texts if text]
    if not texts:
        return []
    query = "\n".join(texts)
    terms = tuple(dict.fromkeys(term for text in texts for term in _terms(text)))
    if not terms:
        return []
    ranked = []
    for kind in _RELATED_KINDS:
        for card in _load_cards(store, scope, kind.value, as_of_ms=as_of_ms, config=config):
            score = _overlap_score(card, query, terms)
            if score > 0:
                ranked.append((score, card))
    ranked.sort(key=lambda row: (-row[0], -row[1].priority, row[1].item_id))
    return [card for _, card in ranked[:config.related_card_limit]]


def _load_cards(store, scope, kind: str, *, as_of_ms=None, config: MemoryExtractionConfig | None = None) -> list:
    config = config or MemoryExtractionConfig()
    chosen = []
    seen: set[str] = set()
    offset = 0
    while offset < config.card_scan_limit:
        page = tuple(store.search(
            scope, "", kind=kind, limit=config.card_page_size, offset=offset, as_of_ms=as_of_ms,
        ))
        fresh = [card for card in page if card.item_id not in seen]
        if not fresh:
            break
        seen.update(card.item_id for card in fresh)
        chosen.extend(fresh)
        if len(page) < config.card_page_size:
            break
        offset += len(page)
    return chosen

### 加权对应卡片的重合度
#### TODO：这样的加权是否合理？
def _overlap_score(card, query: str, terms: tuple[str, ...]) -> int:
    keywords = tuple(
        keyword for keyword in card.keywords
        if _specific_keyword(keyword) and any(
            _contains(keyword, term) or _contains(query, keyword) for term in terms
        )
    )
    summary = card.summary or ""
    applicability = getattr(card, "applicability", None) or ""
    title_hits = sum(_contains(card.title or "", term) for term in terms)
    summary_hits = sum(_contains(summary, term) for term in terms)
    applicability_hits = sum(_contains(applicability, term) for term in terms)
    content_hits = sum(_contains(card.content, term) for term in terms)
    return 6 * len(keywords) + 2 * title_hits + 2 * applicability_hits + summary_hits + content_hits
