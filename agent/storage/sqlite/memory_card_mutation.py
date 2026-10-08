"""正常完成的轮次中，按修订号更正或删除已有卡片。

职责：核验来源消息、scope、证据与目标修订号，提交显式修改。
边界：不能创建新卡或改变类别、事实槽位；手册由独立编辑器维护。
关联：memory_edit 只暂存修改；SessionStore 成功提交轮次时调用。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Mapping

from ...core.memory.memory_item import content_sha256
from ...core.memory.memory_claim_evidence import (
    explicit_user_statement, has_source_overlap, unsupported_anchors,
)
from ...core.memory.memory_scope import MemoryScope
from ...core.memory.memory_validator import MemoryValidator
from ...core.memory.types.card_types import UserCardKind, UserCardStatus
from ...core.memory.types.extraction_types import MemoryProposal
from ...core.memory.types.memory_source_types import EvidenceRef
from ...core.types import Message
from .errors import IntegrityError, RevisionConflictError
from .memory_store import put_user_memory, read_user_memory
from .memory_card_policy import authority_rank, source_is_not_older


class MemoryCardConflictError(ValueError):
    """来源过时或目标卡已变化。"""


class SqliteMemoryCardMutationService:
    def __init__(self, database, *, validator: MemoryValidator | None = None):
        self.db = database
        self.validator = validator or MemoryValidator()

    def apply(
        self, conn, scope: MemoryScope, source_message_id: str,
        payload: Mapping[str, object], *, source_authority: str | None = None,
    ):
        """调用者已确认轮次成功；此处在同一事务内应用暂存的修改。"""
        if not isinstance(payload, Mapping):
            raise ValueError("memory mutation payload must be an object")
        operation = payload.get("operation")
        allowed = {
            "update": {
                "operation", "item_id", "expected_revision", "summary", "content",
                "title", "keywords", "applicability", "action", "outcome",
                "limitations", "outcome_status", "reason_code",
            },
            "delete": {"operation", "item_id", "expected_revision"},
        }
        if operation not in allowed or set(payload) - allowed[operation]:
            raise ValueError("unsupported memory mutation operation or fields")
        item_id = payload.get("item_id")
        revision = payload.get("expected_revision")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("existing item_id is required")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("expected_revision is required")
        source = self._require_user_message(conn, scope, source_message_id)
        item = read_user_memory(conn, item_id, scope_key=scope.storage_key)
        if item is None:
            raise LookupError(item_id)
        if item.revision != revision:
            raise RevisionConflictError("memory revision changed")
        if item.status is not UserCardStatus.ACTIVE:
            raise MemoryCardConflictError("only active cards can be changed")
        if not source_is_not_older(item, source.observed_at):
            raise MemoryCardConflictError("older user message cannot change newer memory")
        if operation == "delete":
            return put_user_memory(conn, item.model_copy(update={
                "status": UserCardStatus.DELETED,
                "evidence": (*item.evidence, source),
                "metadata": {**item.metadata, "deleted_by_message_id": source_message_id},
                "revision": item.revision + 1,
            }), expected_revision=revision)
        summary = payload.get("summary")
        content = payload.get("content")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("card_update requires summary")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("card_update requires content")
        keywords = payload.get("keywords") or item.keywords
        if not isinstance(keywords, (tuple, list)) or any(
            not isinstance(word, str) for word in keywords
        ):
            raise ValueError("keywords must be a text list")
        if item.kind is UserCardKind.REUSABLE_EXPERIENCE:
            subject = attribute = None
        else:
            _, subject, attribute = item.slot_key.split(":", 2)
        values = {
            name: payload.get(name) or getattr(item, name)
            for name in ("applicability", "action", "outcome", "limitations", "outcome_status")
        }
        proposal = MemoryProposal(
            proposal_id=f"edit:{item_id}:{revision}",
            kind=item.kind, subject=subject, attribute=attribute,
            title=payload.get("title") or item.title, summary=summary.strip(),
            content=content.strip(), keywords=tuple(keywords),
            confidence=item.confidence, source_authority=source_authority
            or self._authority_from_message(conn, source_message_id),
            source_message_ids=(source_message_id,),
            evidence_quotes={source_message_id: source.excerpt or ""},
            reason_code=str(payload.get("reason_code") or "explicit_memory_edit"),
            **values,
        )
        validation = self.validator.validate(proposal, scope=scope)
        if not validation.accepted or validation.user_item is None:
            raise ValueError("memory card rejected: " + ",".join(validation.validation_codes))
        if authority_rank(item.source_authority) > authority_rank(proposal.source_authority):
            raise MemoryCardConflictError("model inference cannot overwrite explicit user memory")
        claim = summary + "\n" + content
        evidence_text = source.excerpt or ""
        if not has_source_overlap(claim, evidence_text):
            raise ValueError("memory card claim is not supported by source message")
        if unsupported_anchors(claim, evidence_text):
            raise ValueError("memory card contains a name or number absent from source message")
        candidate = validation.user_item
        if candidate.kind != item.kind or candidate.slot_key != item.slot_key:
            raise MemoryCardConflictError("card category and fact slot cannot change")
        updated = item.model_copy(update={
            "title": candidate.title, "summary": candidate.summary,
            "content": candidate.content, "content_hash": content_sha256(candidate.content),
            "token_count": candidate.token_count, "keywords": candidate.keywords,
            "applicability": candidate.applicability, "action": candidate.action,
            "outcome": candidate.outcome, "limitations": candidate.limitations,
            "outcome_status": candidate.outcome_status,
            "source_authority": candidate.source_authority,
            "source_type": "memory_edit", "source_ref": source_message_id,
            "evidence": (*item.evidence, source), "revision": revision + 1,
            "metadata": {**item.metadata, "edit_reason": proposal.reason_code},
        })
        return put_user_memory(conn, updated, expected_revision=revision)

    @staticmethod
    def _authority_from_message(conn, message_id: str) -> str:
        row = conn.execute("SELECT content_text FROM messages WHERE message_id=?", (message_id,)).fetchone()
        text = row[0] if row else ""
        return "user_explicit" if explicit_user_statement(text) else "model_inferred"

    @staticmethod
    def _require_user_message(conn, scope: MemoryScope, message_id: str) -> EvidenceRef:
        row = conn.execute(
            "SELECT m.payload_json,m.created_at,m.role,s.scope_key FROM messages m "
            "JOIN sessions s ON s.session_id=m.session_id WHERE m.message_id=?",
            (message_id,),
        ).fetchone()
        if row is None or row[2] != "user" or row[3] != scope.storage_key:
            raise IntegrityError("source message missing or outside current user scope")
        message = Message.model_validate_json(row[0])
        if message.metadata.get("source_type") in {"memory_recalled", "system_injected"}:
            raise IntegrityError("injected text cannot authorize a memory edit")
        return EvidenceRef(
            source_type="message", source_id=message_id,
            source_hash=hashlib.sha256(row[0].encode("utf-8")).hexdigest(),
            excerpt=message.content_text,
            observed_at=datetime.fromtimestamp(row[1] / 1000, timezone.utc),
        )



