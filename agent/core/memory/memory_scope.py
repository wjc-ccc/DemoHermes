"""MemoryScope：五层内容共用的显式归属值对象。"""
# 职责：把渠道来源转换为用户/群聊归属，并生成跨会话稳定的存储键。
# 边界：只表示身份，不替调用方完成授权；工具和 Store 必须据此过滤读写。
# 关联：Gateway 构造 scope，Markdown 目录、卡片和原文检索共用 storage_key。
from __future__ import annotations

import json
from pathlib import Path


from agent.types.channel import SessionSource

from agent.types.memory.scope import MemoryScope


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
