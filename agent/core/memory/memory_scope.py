"""MemoryScope：五层内容共用的显式归属值对象。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..types import SessionSource


class MemoryScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None

    def identity_payload(self) -> dict[str, str | None]:
        """返回稳定字段顺序的 scope 身份，不包含 session/turn 临时维度。"""

        return {
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
        }

    @property
    def storage_key(self) -> str:
        payload = json.dumps(
            self.identity_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def scope_kind(self) -> str:
        return "personal" if self.user_id is not None else "group"


def build_memory_scope(source: SessionSource) -> MemoryScope:
    """把平台来源映射为显式租户 / 用户 scope。"""

    channel_tenant = f"{source.channel}:chat:{source.chat_id}"
    if source.chat_type == "dm":
        user_id = source.user_id or f"chat:{source.chat_id}"
        return MemoryScope(tenant_id=source.channel, user_id=user_id)
    return MemoryScope(tenant_id=channel_tenant)


def ensure_scope_dir(root: Path, scope: MemoryScope) -> Path:
    """创建不可由外部 ID 穿越的 scope 目录，并保存可读元数据。"""

    directory = root / scope.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "scope.json"
    if not marker.exists():
        marker.write_text(
            json.dumps(scope.identity_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    return directory
