"""从正常完成的对话批次中选择值得长期保存的用户卡片。

职责：一次调用 LLM 分析固定来源轮次，返回零张或多张有证据的卡片提案。
边界：这里只解析模型输出；Reviewer 校验原文，Committer 裁决版本与冲突。
关联：MemoryReviewWorker 通过 MemoryBatchReviewer 调用；已有卡片摘要由 MemoryStore 提供。
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from pydantic import ValidationError

from agent.types.conversation import Session
from agent.core.memory.retrieval_terms import _contains, _specific_keyword, _terms
from agent.types.memory.categories import UserCardKind
from agent.types.memory.extraction import MemoryExtractionBatch, MemoryExtractionIssue, MemoryProposal, MEMORY_EXTRACTOR_VERSION
from agent.types.memory.source import TurnRecord
from agent.prompt.memoryCardExtraction import memoryCardExtractionPrompt

_STABLE_KINDS = (UserCardKind.PROFILE, UserCardKind.STABLE_PREFERENCE)
_RELATED_KINDS = (UserCardKind.ONGOING_FACT, UserCardKind.REUSABLE_EXPERIENCE)
_RELATED_CARD_LIMIT = 8
_CARD_PAGE_SIZE = 100
_CARD_SCAN_LIMIT = 400


class MemoryExtractionError(Exception):
    """模型调用或整段输出失败；交由作业队列重试。"""

class MemoryExtractor:
    """唯一的后台卡片提取入口；一批轮次调用一次模型。"""

    version = MEMORY_EXTRACTOR_VERSION

    def __init__(
        self,
        model,
        memory_store,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._model = model
        self._memory_store = memory_store
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    ## 从turns和session中调用模型获得记忆卡片提案
    def extract_batch(
        self, turns: tuple[TurnRecord, ...], sessions: tuple[Session, ...],
    ) -> MemoryExtractionBatch:
        """
        主要逻辑：
        1.检验是否turn和session合法性 
        2.缩减进入字段的source
        3.筛选现有的卡片 放入提示词 
        4.模型返回的字段解析后 进行校验 然后构建proposal提案并返回

        extract_batch(turns, sessions)
            只接受同一用户、正常完成的轮次

            sources = 每一轮收成一条:
                用户原文前 1200 字
                该轮未失败的助手回复，每条前 500 字
                该轮工具结果，去掉 memory_*，每条前 500 字

            existing_cards = 身份和偏好全部附上；项目事实和经验只附本批原文命中的最多 8 张
            每张只含 kind、slot_key、summary
            模型一次返回 JSON 数组，可以是 []

            对每一项:
                不是对象、没有来源、引用了本批之外的 id、重复 → 记问题，跳过
                其余成为提案，来源权威先标成 model_inferred

            整段不是 JSON，或模型调用失败 → 整批失败，作业稍后重试
        """
        ### 验证turns和sessions 环境的合法性
        if not turns or len(turns) != len(sessions):
            raise ValueError("提炼批次必须包含对应的轮次与会话")
        if any(not turn.completed or turn.interrupted for turn in turns):
            raise ValueError("只能提炼正常完成的轮次")
        scope_key = turns[0].scope.storage_key
        if any(turn.scope.storage_key != scope_key for turn in turns):
            raise ValueError("批次不能跨用户 scope")
        if self._model is None:
            raise MemoryExtractionError("后台记忆提炼模型不可用")

        ### 获取message_ids和tool_ids  不拿记忆工具返回
        message_ids = {turn.user_message_id for turn in turns}
        tool_ids = {
            tool.tool_call_id
            for turn, session in zip(turns, sessions)
            for tool in session.tool_calls
            if tool.tool_call_id in turn.tool_call_ids
            and not tool.tool_name.startswith("memory_")
        }

        ### 获取user tool assiatnt的信息 然后构建一个source（按需压缩）的列表
        sources = []
        for turn, session in zip(turns, sessions):
            user = next(
                (message for message in session.messages
                 if message.message_id == turn.user_message_id and message.role == "user"),
                None,
            )
            if user is None:
                raise MemoryExtractionError("批次用户消息缺失")
            assistant_ids = set(turn.assistant_message_ids)
            current_tools = set(turn.tool_call_ids)

            ### TODO：压缩这里应该优化 这种截断会丢失关键信息
            sources.append({
                "turn_id": turn.turn_id,
                "session_id": turn.session_id,
                "user_message_id": user.message_id,
                "user_text": (user.content_text or "")[:1200],
                "assistant": [
                    {"message_id": message.message_id, "text": (message.content_text or "")[:500]}
                    for message in session.messages
                    if message.message_id in assistant_ids and message.status != "failed"
                ],
                "tools": [
                    {"tool_call_id": tool.tool_call_id, "name": tool.tool_name,
                     "status": tool.status, "result": (tool.result_text or "")[:500]}
                    for tool in session.tool_calls
                    if tool.tool_call_id in current_tools and tool.tool_call_id in tool_ids
                ],
            })

        ### 从数据库中取出对应的现有卡片 并且做筛选
        existing_cards = _existing_card_views(
            self._memory_store, turns[-1].scope,
            [source["user_text"] for source in sources],
        )
        try:
            response = self._model.chat([
                {"role": "system", "content":memoryCardExtractionPrompt},
                {"role": "user", "content": json.dumps(
                    {"sources": sources, "existing_cards": existing_cards},
                    ensure_ascii=False,
                )},
            ])
        except Exception as exc:
            raise MemoryExtractionError("后台记忆模型调用失败") from exc

        ### 解析模型返回json数组
        raw = getattr(response, "text", None)
        if raw is None and isinstance(response, dict):
            raw = response.get("text") or response.get("content")
        if not isinstance(raw, str):
            raise MemoryExtractionError("模型未返回文本")
        try:
            items = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise MemoryExtractionError("模型输出不是 JSON") from exc
        if not isinstance(items, list):
            raise MemoryExtractionError("模型输出必须是 JSON 数组")

        proposals: list[MemoryProposal] = []
        issues: list[MemoryExtractionIssue] = []
        seen: set[tuple] = set() ## 去重卡片key

        ### 遍历模型输出的卡片提案 进行校验 卡片的来源是否在message idx里 卡片的字段
        for index, item in enumerate(items):
            identity = f"invalid:{index}"
            if not isinstance(item, dict):
                issues.append(MemoryExtractionIssue(
                    proposal_id=identity, code="model_item_not_object"))
                continue
            try:
                ### 判断来源是否存在
                cited_messages = tuple(item.get("source_message_ids") or ())
                cited_tools = tuple(item.get("source_tool_call_ids") or ())
                if not cited_messages and not cited_tools:
                    raise ValueError("卡片没有来源 ID")
                if not set(cited_messages) <= message_ids or not set(cited_tools) <= tool_ids:
                    raise ValueError("卡片引用了批次外来源")
                

                payload = dict(item)
                payload["proposal_id"] = self._id_factory()
                payload["kind"] = UserCardKind(str(item["kind"]))
                payload["source_authority"] = "model_inferred"
                payload["source_message_ids"] = cited_messages
                payload["source_tool_call_ids"] = cited_tools
                payload["reason_code"] = str(item.get("reason_code") or "model_proposal")[:100]
                proposal = MemoryProposal.model_validate(payload)## 自动运行after hook
                key = (
                    proposal.kind, proposal.slot_key or proposal.content,
                    proposal.source_message_ids, proposal.source_tool_call_ids,
                )
                if key in seen:
                    issues.append(MemoryExtractionIssue(
                        proposal_id=identity, code="duplicate_model_item"))
                    continue
                seen.add(key)
                proposals.append(proposal)
            except (KeyError, TypeError, ValueError, ValidationError):
                issues.append(MemoryExtractionIssue(
                    proposal_id=identity, code="invalid_model_item"))

        ### 最终返回批量记忆提取的结果 问题卡片
        return MemoryExtractionBatch(proposals=tuple(proposals), issues=tuple(issues))


def _existing_card_views(store, scope, user_texts: list[str]) -> list[dict]:
    """身份和偏好供槽位对齐；项目事实和经验只带本批原文命中的少数卡片。"""
    stable = []
    for kind in _STABLE_KINDS:
        stable.extend(_load_cards(store, scope, kind.value))
    seen: set[str] = set()
    views = []
    for card in (*stable, *_related_cards(store, scope, user_texts)):
        if card.item_id in seen:
            continue
        seen.add(card.item_id)
        views.append({
            "kind": card.kind.value,
            "slot_key": card.slot_key,
            "summary": (card.summary or card.content or "")[:250],
        })
    return views


def _related_cards(store, scope, user_texts: list[str]) -> list:
    texts = [text for text in user_texts if text]
    if not texts:
        return []
    query = "\n".join(texts)[:8000]
    terms = tuple(dict.fromkeys(term for text in texts for term in _terms(text[:1200])))[:32]
    if not terms:
        return []
    ranked = []
    for kind in _RELATED_KINDS:
        for card in _load_cards(store, scope, kind.value):
            score = _overlap_score(card, query, terms)
            if score > 0:
                ranked.append((score, card))
    ranked.sort(key=lambda row: (-row[0], -row[1].priority, row[1].item_id))
    return [card for _, card in ranked[:_RELATED_CARD_LIMIT]]


def _load_cards(store, scope, kind: str) -> list:
    chosen = []
    seen: set[str] = set()
    offset = 0
    while offset < _CARD_SCAN_LIMIT:
        page = tuple(store.search(
            scope, "", kind=kind, limit=_CARD_PAGE_SIZE, offset=offset,
        ))
        fresh = [card for card in page if card.item_id not in seen]
        if not fresh:
            break
        seen.update(card.item_id for card in fresh)
        chosen.extend(fresh)
        if len(page) < _CARD_PAGE_SIZE:
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
