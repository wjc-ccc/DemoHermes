"""面向模型的卡片修改与个人手册编辑工具。

职责：校验操作参数和运行时 scope；卡片修改暂存，手册修改交给受约束编辑器。
边界：不直接决定后台提炼内容；卡片暂存结果须由 SessionStore 随轮次提交。
关联：AgentLoop 注入当前用户消息 ID，MemoryCardMutationService 与 MemoryManualEditor 执行写入。
"""
from __future__ import annotations

import json

from ..core.memory.markdown_book import MarkdownRevisionConflictError
from ..core.memory.markdown_editor import MemoryManualError, MemoryManualEditor
from ..core.memory.memory_scope import MemoryScope
from ..storage.sqlite.memory_store import SqliteMemoryStore
from .base import Tool, ToolCallResult


_EXPERIENCE_FIELDS = ("applicability", "action", "outcome", "limitations", "outcome_status")


class MemoryEditTool(Tool):
    name = "memory_edit"
    description = (
        "更正、删除当前用户已有的记忆卡片，或读取、替换、恢复当前用户的 MEMORY.md。"
        "不能新建卡片：用户陈述的新信息由后台自动提炼，无需调用本工具。"
        "card_update/card_delete 先用 memory_search 找到卡片，item_id 与 expected_revision "
        "取自命中结果；事实卡只改 title、summary、content、keywords。"
        "卡片修改在本轮对话正常完成时一起落库；完成后才能确认已更正或删除。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": [
                "card_update", "card_delete", "manual_get", "manual_replace", "manual_restore"
            ]},
            "item_id": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "content": {"type": "string", "description": "单一、可独立更正的事实；姓名、产品名和数字必须能在当前消息或证据原文中找到"},
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "applicability": {"type": "string"},
            "action": {"type": "string"},
            "outcome": {"type": "string"},
            "limitations": {"type": "string"},
            "outcome_status": {"type": "string", "enum": ["success", "failure", "mixed", "observed"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            "expected_hash": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "reason": {"type": "string"},
        },
        "required": ["operation"],
    }

    def __init__(self, cards: SqliteMemoryStore, manual: MemoryManualEditor) -> None:
        self._cards = cards
        self._manual = manual

    def execute(self, arguments: dict) -> ToolCallResult:
        scope = arguments.get("_scope")
        source_message_id = arguments.get("_source_message_id")
        if not isinstance(scope, MemoryScope) or scope.user_id is None:
            return ToolCallResult(ok=False, error="缺少当前用户的 memory scope")
        operation = arguments.get("operation")
        if operation in {"card_update", "card_delete"}:
            if not isinstance(source_message_id, str) or not source_message_id:
                return ToolCallResult(ok=False, error="缺少当前用户消息引用")
            try:
                payload = self._stage_card(operation, arguments, scope)
            except (ValueError, TypeError) as exc:
                return ToolCallResult(ok=False, error=str(exc))
            return ToolCallResult(
                ok=True,
                result_text="卡片修改已接收，将在本轮对话提交时确认。",
                data={"memory_mutation": payload},
            )
        try:
            if operation == "manual_get":
                current = self._manual.read(scope)
                result = {
                    "content": current.snapshot.content,
                    "hash": current.disk_hash,
                    "revision": current.revision,
                    "recommended_headings": ["个人资料", "沟通与工作偏好", "长期关注"],
                    "diagnostics": [diagnostic.model_dump() for diagnostic in current.diagnostics],
                }
            elif operation == "manual_replace":
                content = arguments.get("content")
                expected_hash = arguments.get("expected_hash")
                reason = arguments.get("reason")
                if (not isinstance(content, str)
                    or not isinstance(expected_hash, str) or not expected_hash
                    or not isinstance(reason, str) or not reason):
                    raise ValueError("manual_replace 需要 content、expected_hash 和 reason")
                snapshot = self._manual.replace(
                    scope, content, expected_hash=expected_hash,
                    actor="model", reason=reason,
                    source_ref=f"message:{source_message_id}" if source_message_id else None,
                )
                result = {"hash": snapshot.content_hash, "token_count": snapshot.token_count}
            elif operation == "manual_restore":
                expected_hash = arguments.get("expected_hash")
                reason = arguments.get("reason")
                revision = arguments.get("revision")
                if not isinstance(expected_hash, str) or not expected_hash:
                    raise ValueError("manual_restore 需要 expected_hash")
                if not isinstance(reason, str) or not reason:
                    raise ValueError("manual_restore 需要 reason")
                snapshot = self._manual.restore(
                    scope, int(revision), expected_hash=expected_hash,
                    actor="model", reason=reason,
                    source_ref=f"message:{source_message_id}" if source_message_id else None,
                )
                result = {"hash": snapshot.content_hash, "token_count": snapshot.token_count}
            else:
                return ToolCallResult(ok=False, error="未知 memory_edit operation")
        except (MemoryManualError, MarkdownRevisionConflictError, ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        return ToolCallResult(ok=True, result_text=json.dumps(result, ensure_ascii=False), data=result)

    def _stage_card(self, operation: str, arguments: dict, scope: MemoryScope) -> dict:
        item_id = str(arguments.get("item_id") or "").strip().removeprefix("card:")
        if not item_id:
            raise ValueError(f"{operation} 需要已有卡片的 item_id；新信息无需调用工具，后台会自动提炼")
        existing = self._cards.get(item_id)
        if existing is None or existing.scope.storage_key != scope.storage_key:
            raise ValueError("卡片不存在或不属于当前用户")
        if existing.status.value != "active":
            raise ValueError("只能修改当前有效的卡片")
        revision = self._revision(arguments)
        if revision != existing.revision:
            raise ValueError("卡片修订号已变化，请重新读取")
        if operation == "card_delete":
            return {
                "operation": "delete", "item_id": item_id,
                "expected_revision": revision,
            }
        if any(name in arguments for name in ("kind", "subject", "attribute", "supersedes_item_id")):
            raise ValueError("card_update 不能改变卡片类别或事实槽位")
        experience_fields = [name for name in _EXPERIENCE_FIELDS if arguments.get(name)]
        if existing.kind.value != "reusable_experience" and experience_fields:
            raise ValueError("事实卡只能修改 title、summary、content、keywords，不能填写 "
                             + "、".join(experience_fields))
        required = ("summary", "content")
        fields = {name: str(arguments.get(name) or "").strip() for name in required}
        if any(not value for value in fields.values()):
            raise ValueError("card_update 需要 summary、content")
        if len(fields["content"]) > 4000:
            raise ValueError("卡片键或正文过长")
        keywords = arguments.get("keywords") or []
        if not isinstance(keywords, list) or not all(isinstance(word, str) for word in keywords):
            raise ValueError("keywords 必须是字符串数组")
        return {
            "operation": "update", "item_id": item_id,
            "summary": fields["summary"], "content": fields["content"],
            "title": arguments.get("title"), "keywords": keywords,
            "applicability": arguments.get("applicability"),
            "action": arguments.get("action"), "outcome": arguments.get("outcome"),
            "limitations": arguments.get("limitations"),
            "outcome_status": arguments.get("outcome_status"),
            "expected_revision": revision,
            "reason_code": str(arguments.get("reason") or "model_memory_edit")[:100],
        }

    @staticmethod
    def _revision(arguments: dict) -> int:
        value = arguments.get("expected_revision")
        if value is None:
            raise ValueError("卡片修改需要 expected_revision，取 memory_search 结果中的 revision")
        revision = int(value)
        if revision < 1:
            raise ValueError("expected_revision 必须大于 0")
        return revision
