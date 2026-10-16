"""session_key 确定性公式 — 全项目唯一实现点。"""
from __future__ import annotations

from agent.types.channel import SessionSource


def build_session_key(source: SessionSource, agent_id: str = "main") -> str:
    """
    DM:    agent:{id}:{channel}:dm:{chat_id}[:user:{uid}][:thread:{tid}]
    Group: agent:{id}:{channel}:group:{chat_id}[:user:{uid}][:thread:{tid}]

    DM 与 Group 都带 user：build_memory_scope 的 DM scope 按 user_id 归属，
    同一槽位只能对应一个 scope，否则 SessionStore 会拒绝这个槽位。
    """
    parts = [f"agent:{agent_id}", source.channel, source.chat_type, source.chat_id]
    if source.chat_type in ("dm", "group") and source.user_id:
        parts.append(f"user:{source.user_id}")
    if source.thread_id:
        parts.append(f"thread:{source.thread_id}")
    return ":".join(parts)


def parse_session_key(session_key: str) -> SessionSource | None:
    """build_session_key 的逆运算；无法唯一还原（如 chat_id 含冒号）时返回 None。"""
    parts = (session_key or "").split(":")
    if len(parts) < 5 or parts[0] != "agent" or parts[3] not in ("dm", "group", "thread"):
        return None
    agent_id, channel, chat_type, chat_id = parts[1:5]
    fields: dict[str, str] = {}
    rest = parts[5:]
    while rest:
        if len(rest) < 2 or rest[0] not in ("user", "thread") or rest[0] in fields:
            return None
        fields[rest[0]] = rest[1]
        rest = rest[2:]
    source = SessionSource(
        channel=channel, chat_id=chat_id, chat_type=chat_type,
        user_id=fields.get("user"), thread_id=fields.get("thread"),
    )
    return source if build_session_key(source, agent_id=agent_id) == session_key else None
