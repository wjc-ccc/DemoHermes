"""EnvironmentSnapshotter — Slice 15 环境快照最小实现（8.04 Phase 5.2）。

捕获 cwd / git / runtime（model、prompt hash、tool schema、skill 版本），
便于对比进化前后；不宣称可精确复现完整 LLM payload。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    session_id: str
    turn_id: str
    captured_at: float
    cwd: str | None = None
    git: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(raw)


class EnvironmentSnapshotter:
    def __init__(self, executions) -> None:
        self._executions = executions

    def capture(
        self,
        *,
        session_id: str,
        turn_id: str,
        cwd: str | None = None,
        model_name: str | None = None,
        system_prompt_path: Path | None = None,
        tool_schemas: list[dict] | None = None,
        skill_versions: dict[str, str] | None = None,
        git: dict[str, Any] | None = None,
    ) -> EnvironmentSnapshot:
        prompt_hash = None
        if system_prompt_path is not None and Path(system_prompt_path).is_file():
            prompt_hash = _sha256_text(
                Path(system_prompt_path).read_text(encoding="utf-8")
            )
        runtime = {
            "model_name": model_name,
            "prompt_hash": prompt_hash,
            "tool_schema_hash": _sha256_json(tool_schemas or []),
            "skill_versions": dict(skill_versions or {}),
        }
        snap = EnvironmentSnapshot(
            snapshot_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            turn_id=turn_id,
            captured_at=time.time(),
            cwd=cwd,
            git=dict(git or {}),
            runtime=runtime,
        )
        self._executions.put_environment(snap)
        return snap

    def get(self, snapshot_id: str) -> EnvironmentSnapshot | None:
        data = self._executions.get_environment(snapshot_id)
        return EnvironmentSnapshot.model_validate(data) if data is not None else None
