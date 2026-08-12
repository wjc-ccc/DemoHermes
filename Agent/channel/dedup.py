"""
TTL 消息去重 — Hermes helpers.MessageDeduplicator 的精简移植

平台重推同一条消息（webhook 重试 / ws 重连重放）时防止 Agent 重复处理。
key 首次见到返回 False 并记录；TTL 内再次见到返回 True。
"""
from __future__ import annotations

import threading
import time


class TTLDedup:
    """线程安全的 TTL 去重缓存（默认 300s / 1000 条上限）。"""

    def __init__(self, ttl: float = 300.0, maxsize: int = 1000) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_dup(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        with self._lock:
            self._purge(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            if len(self._seen) > self.maxsize:
                # 超上限：淘汰最旧的一批（保留最近 80%）
                for old in sorted(self._seen, key=self._seen.get)[: self.maxsize // 5]:
                    self._seen.pop(old, None)
            return False

    def _purge(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self.ttl]
        for k in expired:
            self._seen.pop(k, None)

    def __len__(self) -> int:
        return len(self._seen)
