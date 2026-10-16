"""面向模型的卡片修改与个人手册编辑工具。

职责：校验操作参数和运行时 scope；卡片修改暂存，手册修改交给受约束编辑器。
边界：不直接决定后台提炼内容；卡片暂存结果须由 SessionStore 随轮次提交。
关联：AgentLoop 注入当前用户消息 ID，MemoryCardMutationService 与 MarkdownDocuments 执行写入。
"""
from __future__ import annotations

import json

from agent.types.memory.document import MarkdownRevisionConflictError
from agent.types.memory.document import MemoryManualError
from agent.core.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from ..storage.sqlite.memory_store import SqliteMemoryStore
from agent.core.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext
from agent.types.memory.mutation import MemoryMutationItem


_EXPERIENCE_FIELDS = ("applicability", "action", "outcome", "limitations", "outcome_status")


class MemoryEditTool(Tool):
    name = "memory_edit"
    description = (
        "更正、删除当前用户已有的记忆卡片，或读取、替换、恢复当前用户的 MEMORY.md。"
        "不能新建卡片：用户陈述的新信息由后台自动提炼，无需调用本工具。"
        "仅询问已记录信息、比较历史值、请你回忆或总结时，只检索读取，不执行 card_update/card_delete。"
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
    action_policy = ToolPolicy(when_to_use="用户明确更正已有记忆或个人手册",
        when_not_to_use="不用于创建事实卡或改变类别",
        effect="write", risk="medium", permission="user", idempotency="keyed",
        key_fields=("operation", "item_id", "expected_revision", "expected_hash", "revision"))
    # 卡片改动随本轮提交一起落库；手册改动按 hash 前置条件，即时生效。
    _card = action_policy.model_copy(update={"precondition_field": "expected_revision", "deferred_commit": True})
    _manual = action_policy.model_copy(update={"precondition_field": "expected_hash"})
    action_operations = {
        "manual_get": ToolPolicy(when_to_use=description, when_not_to_use="不用于产生业务写入"),
        "card_update": _card, "card_delete": _card,
        "manual_replace": _manual, "manual_restore": _manual,
    }

    def __init__(self, cards: SqliteMemoryStore, manual: MarkdownDocuments) -> None:
        self._cards = cards
        self._manual = manual

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        scope = context.memory_scope
        source_message_id = context.source_message_id
        if not isinstance(scope, MemoryScope) or scope.user_id is None:
            return ToolCallResult(ok=False, error="缺少当前用户的 memory scope")
        operation = arguments.get("operation")
        if isinstance(operation, str) and operation.startswith("manual_"):
            unsupported = set(arguments) - set(self.parameters["properties"])
            if unsupported:
                return ToolCallResult(ok=False, error="个人手册不接受路径、目标身份或其他未声明参数")
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
                pending_mutation=MemoryMutationItem.model_validate(payload),
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
                snapshot = self._manual.replace_user(
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
                snapshot = self._manual.restore_user(
                    scope, int(revision), expected_hash=expected_hash,
                    actor="model", reason=reason,
                    source_ref=f"message:{source_message_id}" if source_message_id else None,
                )
                result = {"hash": snapshot.content_hash, "token_count": snapshot.token_count}
            else:
                return ToolCallResult(ok=False, error="未知 memory_edit operation")
        except (MemoryManualError, MarkdownRevisionConflictError, ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        from agent.core.prompt.renderers import render_tool_result
        return ToolCallResult(ok=True, result_text="手册操作已提交。\n" + render_tool_result(self.name, result), data=result)

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
