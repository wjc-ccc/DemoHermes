"""Session 增量到 MemoryProposal 的生产 Extractor。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..types import Session
from .memory_layer import MemoryLayer
from .types import MemoryProposal, ProposedMutation, TurnRecord
from .memory_item import UserMemoryKind

logger = logging.getLogger(__name__)

_PREFERENCE_MARKERS = (
    "我偏好",
    "我喜欢",
    "请记住",
    "记住我",
    "以后请",
    "以后都",
    "不要再",
    "我叫",
    "我的名字",
    "我习惯",
    "以后叫我",
)
_ASSISTANT_CONSTRAINT_MARKERS = (
    "你必须",
    "请始终",
    "作为助手",
    "回答时请",
    "以后回答",
)
_TASK_EVENT_MARKERS = (
    "已完成",
    "完成了",
    "发布了",
    "部署了",
    "修复了",
    "决定",
    "下一步",
    "被阻塞",
)
_GREETINGS = {"你好", "您好", "hi", "hello", "hey", "在吗", "早上好", "晚上好"}
_ONE_OFF_PREFIXES = ("帮我", "请帮我", "生成", "解释", "翻译", "告诉我", "计算")
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token)\b"),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"(?:密码|密钥|身份证号|访问令牌)"),
)
_QUOTED_CONTEXT_PATTERNS = (
    re.compile(r"(?:文档|文章|提示词|系统提示|别人|他说|她说).{0,12}(?:写着|提到|说)[：:\s]*[“\"']?"),
    re.compile(r"^引用[：:\s]"),
)

_SYSTEM_PROMPT = """你是记忆提案器，只输出 JSON 数组。
只能提案 user 或 task 层，不能写 policy/project/scratchpad。
user.type 只能是 identity/preference/capability/tenant_config/relationship/long_term_fact/reusable_experience。
task.type 只能是 goal/milestone/progress/checkpoint/decision/blocker/next_step/rejected_option。
临时请求、招呼、未经证实的敏感属性、密钥、工具原始流水不提案。
用户稳定偏好与明确身份进入 user；本任务目标、进度、决定与下一步进入 task。
每项字段：target_layer,type,memory_kind,normalized_key,title,summary,content,keywords,confidence,source_tool_call_ids,proposed_mutation,supersedes_item_id,reason_code。
memory_kind 仅 user 可用 episodic/semantic/procedural；proposed_mutation 默认 create。
不要输出 markdown 围栏或解释。"""


def _normalized_greeting(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:-1] if normalized.endswith(("呀", "啊", "哦")) else normalized


def _skip(text: str) -> bool:
    if not text or text.startswith("/") or len(text) < 4:
        return True
    if _normalized_greeting(text) in _GREETINGS:
        return True
    if text.endswith(("?", "？")) or text.startswith(("如果", "假如", "假设")):
        return True
    if any(pattern.search(text) for pattern in _QUOTED_CONTEXT_PATTERNS):
        return True
    if any(text.startswith(prefix) for prefix in _ONE_OFF_PREFIXES):
        return True
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def _key(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:16]
    return f"{prefix}.{digest}"


def _user_message(turn: TurnRecord, session: Session):
    return next(
        (message for message in session.messages if message.message_id == turn.user_message_id),
        None,
    )

## 用户记忆区分身份和情景
### 这里的关键词逻辑也应该合理设置 
### 不同的助手应该设定不同的关键词 用于提取不同类别记忆
class RuleBasedMemoryExtractor:
    """保守降级 Extractor：只处理显式偏好、身份和明确任务进展。"""

    version = "rule-memory-extractor-v1"

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def extract(self, turn: TurnRecord, session: Session) -> list[MemoryProposal]:
        if turn.interrupted or not turn.completed:
            return []
        message = _user_message(turn, session)
        if message is None:
            return []
        text = (message.content_text or "").strip()
        if _skip(text):
            return []

        if any(marker in text for marker in ("我叫", "我的名字", "以后叫我")):
            return [self._user_proposal(turn, text, "identity", "identity.display_name")]
        if any(marker in text for marker in _PREFERENCE_MARKERS) or any(
            marker in text for marker in _ASSISTANT_CONSTRAINT_MARKERS
        ):
            normalized_key = (
                "preference.response_language"
                if any(language in text for language in ("中文", "英文"))
                else "preference.response_detail"
                if any(detail in text for detail in ("详细", "简洁", "结论"))
                else _key("preference", text)
            )
            return [self._user_proposal(turn, text, "preference", normalized_key)]
        
        ## 任务记忆
        if turn.scope.task_id and any(marker in text for marker in _TASK_EVENT_MARKERS):
            item_type = (
                "decision"
                if "决定" in text
                else "next_step"
                if "下一步" in text
                else "blocker"
                if "阻塞" in text
                else "progress"
            )
            return [
                MemoryProposal(
                    proposal_id=self._id_factory(),
                    target_layer=MemoryLayer.TASK,
                    type=item_type,
                    normalized_key=_key(f"task.{item_type}", text),
                    summary=text,
                    content=text,
                    keywords=(),
                    confidence=1.0,
                    source_authority="user_explicit",
                    source_message_ids=(turn.user_message_id,),
                    reason_code=f"explicit_task_{item_type}",
                )
            ]
        return []

    def _user_proposal(
        self, turn: TurnRecord, text: str, item_type: str, normalized_key: str
    ) -> MemoryProposal:
        return MemoryProposal(
            proposal_id=self._id_factory(),
            target_layer=MemoryLayer.USER,
            type=item_type,
            memory_kind=UserMemoryKind.SEMANTIC,
            normalized_key=normalized_key,
            summary=text,
            content=text,
            keywords=(),
            confidence=1.0,
            source_authority="user_explicit",
            source_message_ids=(turn.user_message_id,),
            reason_code=f"explicit_user_{item_type}",
        )


class ModelMemoryExtractionError(Exception):
    pass

### 把历史turn和session存入
class ModelMemoryExtractor:
    """模型只生成 Proposal；权威、证据和落库决定仍由运行时控制。"""

    version = "model-memory-extractor-v1"

    def __init__(
        self,
        model,
        *,
        id_factory: Callable[[], str] | None = None,
        context_messages: int = 8,
    ) -> None:
        self._model = model
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._context_messages = context_messages

    def extract(self, turn: TurnRecord, session: Session) -> list[MemoryProposal]:
        if turn.interrupted or not turn.completed:
            return []
        user = _user_message(turn, session)
        if user is None or _skip((user.content_text or "").strip()):
            return []
        context = "\n".join(
            f"{message.role}: {(message.content_text or '').strip()[:400]}"
            for message in session.messages[-self._context_messages :]
            if (message.content_text or "").strip()
        )
        tools = [
            {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": call.status,
                "result": (call.result_text or "")[:400],
            }
            for call in session.tool_calls
            if call.tool_call_id in set(turn.tool_call_ids)
        ]
        payload = json.dumps(
            {
                "task_id": turn.scope.task_id,
                "user_message_id": turn.user_message_id,
                "allowed_tool_calls": tools,
                "context": context,
            },
            ensure_ascii=False,
        )
        try:
            response = self._model.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ]
            )
        except Exception as exc:
            raise ModelMemoryExtractionError(repr(exc)) from exc
        raw = getattr(response, "text", None)
        if raw is None and isinstance(response, dict):
            raw = response.get("text") or response.get("content")
        items = _parse_array(raw)
        proposals: list[MemoryProposal] = []
        try:
            for item in items:
                proposals.append(self._proposal_from(item, turn))
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ModelMemoryExtractionError(f"提案字段非法: {exc}") from exc
        return proposals

    def _proposal_from(self, item: dict[str, Any], turn: TurnRecord) -> MemoryProposal:
        target = MemoryLayer(str(item["target_layer"]))
        if target not in {MemoryLayer.USER, MemoryLayer.TASK}:
            raise ValueError("target_layer 只允许 user/task")
        tool_ids = tuple(str(value) for value in item.get("source_tool_call_ids", ()))
        memory_kind = item.get("memory_kind")
        
        return MemoryProposal(
            proposal_id=self._id_factory(),
            target_layer=target,
            type=str(item["type"]),
            memory_kind=UserMemoryKind(str(memory_kind)) if memory_kind else None,
            normalized_key=str(item["normalized_key"]).strip(),
            title=str(item["title"]).strip() if item.get("title") else None,
            summary=str(item["summary"]).strip(),
            content=str(item["content"]).strip(),
            keywords=tuple(str(value).strip() for value in item.get("keywords", ())),
            confidence=float(item.get("confidence", 0.0)),
            source_authority="model_inferred",
            source_message_ids=(turn.user_message_id,),
            source_tool_call_ids=tool_ids,
            proposed_mutation=ProposedMutation(
                str(item.get("proposed_mutation", "create"))
            ),
            supersedes_item_id=item.get("supersedes_item_id"),
            reason_code=str(item.get("reason_code", "model_proposal"))[:100],
        )


class FallbackMemoryExtractor:
    """Primary 失败才降级；合法空提案不会触发 fallback。"""

    def __init__(self, primary, fallback) -> None:
        self._primary = primary
        self._fallback = fallback
        self.version = f"{primary.version}+fallback:{fallback.version}"

    def extract(self, turn: TurnRecord, session: Session) -> list[MemoryProposal]:
        try:
            return self._primary.extract(turn, session)
        except ModelMemoryExtractionError as exc:
            logger.warning("模型记忆提取失败，降级到规则提取: %s", exc)
            return self._fallback.extract(turn, session)


def _parse_array(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        raise ModelMemoryExtractionError(f"模型返回非文本: {type(raw)!r}")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelMemoryExtractionError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ModelMemoryExtractionError("模型输出必须是对象数组")
    return data
