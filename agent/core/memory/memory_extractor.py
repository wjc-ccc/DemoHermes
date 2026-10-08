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

from ..types import Session
from .memory_retrieval import _contains, _specific_keyword, _terms
from .types.card_types import UserCardKind
from .types.extraction_types import (
    MemoryExtractionBatch,
    MemoryExtractionIssue,
    MemoryProposal,
)
from .types.memory_source_types import TurnRecord


_STABLE_KINDS = (UserCardKind.PROFILE, UserCardKind.STABLE_PREFERENCE)
_RELATED_KINDS = (UserCardKind.ONGOING_FACT, UserCardKind.REUSABLE_EXPERIENCE)
_RELATED_CARD_LIMIT = 8
_CARD_PAGE_SIZE = 100
_CARD_SCAN_LIMIT = 400


_PROMPT = """你是个人助手的长期记忆提炼器。只返回 JSON 数组，允许返回 []。
仅从本批成功完成的对话中选择未来有使用价值、且能被逐字证据支持的信息。
每张卡只写一条能独立更正的用户事实，或一次有完整结果的经验。
kind 只能是 profile、stable_preference、ongoing_fact、reusable_experience：
profile 是用户本人相对稳定的身份、能力或关系，姓名和希望被如何称呼也属于 profile（attribute 用 display_name）；
stable_preference 是明确的长期沟通或工作偏好，不能把一次请求泛化；
ongoing_fact 是持续有效、可随时间更正的项目背景或约定；
reusable_experience 是特定条件下的做法与真实观察结果。
profile 和 stable_preference 只描述用户本人：subject 写 user，针对助手回复方式的偏好可写 assistant。
同事、家人、朋友等第三方的身份、职业和习惯不保存，即使是用户主动讲述的。
existing_cards 是已保存的卡片。新信息更正或替代已有事实卡时，subject 和 attribute
必须与该卡 slot_key 的后两段完全相同，content 只写更正后的新值；与已有卡含义相同的信息不再提案。
用户要求删除、忘记某条信息时，不为该信息、也不为“用户要求遗忘”或“用户不再做某事”生成卡片。
分类边界：ongoing_fact 保存项目的用途、长期背景或约定，不保存某一次操作的执行状态。
如果信息包含“在什么条件下做了什么、实际出现什么结果”，应评估为 reusable_experience，
不能仅因为出现项目名称就放入 ongoing_fact。同一次实验的条件、做法和结果保存在一张经验卡中。
单次观察也可以形成经验；“只做过一次、尚未验证其他环境”应保留在 limitations 中，不是拒绝经验的理由。
成功、失败、部分成功部分失败和不作成败判断的观察分别用 success、failure、mixed、observed。
只有猜想、建议、计划或尚未执行的动作没有实际结果，不能生成经验卡。
不要保存寒暄、临时请求、待办、过程流水、助手给出的建议或自行声称的事实；
密码、密钥、令牌和敏感号码即使用户要求记住也不提案。
用户对自己的疑问、犹豫或尚未决定的打算不是事实。
失败经验可以保存，但不能写成成功经验；证据不足就不提案，不编造字段。
不得把“其他环境”等概括扩写成原文没有出现的具体系统、技术名称、数字或结论。
名称和数字保持原文写法，例如原文写“两秒”就写“两秒”，不要改写成“2 秒”。
事实卡需要 subject 和 attribute，事实取值只放在 content；经验卡不占事实槽位。
经验卡必须给出 applicability、action、outcome、limitations、outcome_status，
以及 3 到 8 个具体关键词。outcome_status 为 success/failure/mixed/observed。
每项字段：kind,subject,attribute,title,summary,content,keywords,confidence,
source_message_ids,source_tool_call_ids,evidence_quotes,applicability,action,
outcome,limitations,outcome_status,reason_code。
每项必须引用本批用户消息 ID 或工具调用 ID；助手回复只能用于理解上下文。
evidence_quotes 的每个值必须逐字出现在对应原文中。
输出类型必须严格符合以下约定：confidence 是 0 到 1 的 JSON 数字（如 0.9），不能写 high/medium/low。
keywords、source_message_ids、source_tool_call_ids 都是字符串数组，缺省时用 []。
evidence_quotes 是 JSON 对象，键是引用的真实来源 ID，值是该来源中的逐字引文，不能输出引文数组。
事实卡的 applicability/action/outcome/limitations/outcome_status 省略或为 null。
经验卡的这五个字段必须填写；subject/attribute 省略或为 null。
下面仅演示格式，不能把示例事实或示例 ID 当成本批证据：
[{"kind":"profile","subject":"user","attribute":"profession",
"title":"用户职业","summary":"用户是后端工程师","content":"用户是后端工程师。",
"keywords":["后端工程师"],"confidence":0.9,"source_message_ids":["实际用户消息ID"],
"source_tool_call_ids":[],"evidence_quotes":{"实际用户消息ID":"我是后端工程师。"},
"reason_code":"explicit_profession"}]
经验格式示例（同样只演示格式）：
[{"kind":"reusable_experience","title":"一次图片导出观察",
"summary":"在图片导出时改用 PNG 格式后成功","content":"本次图片导出改用 PNG 格式后成功。",
"keywords":["图片导出","PNG","格式转换"],"confidence":0.9,
"applicability":"本次图片导出","action":"改用 PNG 格式","outcome":"导出成功",
"limitations":"只验证了这一次操作","outcome_status":"success",
"source_message_ids":["实际用户消息ID"],"source_tool_call_ids":[],
"evidence_quotes":{"实际用户消息ID":"这次图片导出改用 PNG 格式后成功了，只验证了这一次。"},
"reason_code":"observed_export_result"}]
没有符合条件的卡片就返回 []。不要输出 Markdown 或解释。"""


class MemoryExtractionError(Exception):
    """模型调用或整段输出失败；交由作业队列重试。"""


class MemoryExtractor:
    """唯一的后台卡片提取入口；一批轮次调用一次模型。"""

    version = "memory-extractor-v6"

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
                {"role": "system", "content": _PROMPT},
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
