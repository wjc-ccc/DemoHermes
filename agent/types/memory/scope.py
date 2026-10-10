"""memory / scope 数据契约。"""
from __future__ import annotations
import hashlib
import json
from pydantic import BaseModel, ConfigDict


## 用户级别的身份认证   用于 session  turn   memory等等字段的scope认证
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
