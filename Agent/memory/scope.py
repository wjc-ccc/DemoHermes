"""
MemoryScope → 磁盘目录解析（7.23 方案 §3.1）

- 目录名使用 sha256(f"{kind}:{key}") 完整 hex，外部 ID 永不直接拼进路径，
  既避免平台 ID 碰撞，也阻止 ../../ 路径穿越；
- 每个 scope 目录落一份 scope.json 保存可读标识（operator seam，§2-E）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..core.types import SessionSource
from .types import MemoryScope


def build_memory_scope(source: SessionSource) -> MemoryScope:
    """唯一 scope 构造函数（§3.1）。

    - DM：personal，key 为 "{channel}:user:{user_id}"；无 user_id 回退 "{channel}:chat:{chat_id}"；
    - Group/Thread：group，key 为 "{channel}:chat:{chat_id}"；
    - thread_id 默认不拆分长期记忆；
    - 首版无跨平台身份绑定，相同自然人在不同 channel 默认隔离（ADR-10）。
    """
    if source.chat_type == "dm":
        if source.user_id:
            return MemoryScope(kind="personal", key=f"{source.channel}:user:{source.user_id}")
        return MemoryScope(kind="personal", key=f"{source.channel}:chat:{source.chat_id}")
    return MemoryScope(kind="group", key=f"{source.channel}:chat:{source.chat_id}")


def scope_dir_name(scope: MemoryScope) -> str:
    return hashlib.sha256(f"{scope.kind}:{scope.key}".encode("utf-8")).hexdigest()


def ensure_scope_dir(root: Path, scope: MemoryScope) -> Path:
    """返回该 scope 的存储目录；首次创建时写入 scope.json。"""
    directory = root / scope_dir_name(scope)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "scope.json"
    if not marker.exists():
        marker.write_text(
            json.dumps({"kind": scope.kind, "key": scope.key}, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    return directory
