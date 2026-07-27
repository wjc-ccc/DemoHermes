"""MemoryReviewer - 自动后台提炼（7.23 方案 §8.2）。

从 SessionStore outbox 原子 claim pending review job，调用注入的 MemoryExtractor
产出 MemoryMutation，经 Manager.apply 落库；成功标 done，失败有界退避回 pending，
超 max_attempts 标 failed。Reviewer 不直接写文件，只调 Manager（§8.2）。

模型抽取器是外部 seam，测试用 FakeExtractor 返回固定 mutation；不 Fake Reviewer 本身。
"""
from __future__ import annotations

import time
from typing import Protocol

from .types import MemoryMutation, TurnEnvelope


class MemoryExtractor(Protocol):
    """模型抽取 seam（§8.2）；输入不可变 TurnEnvelope，输出零个或多个 MemoryMutation。"""

    def extract(self, turn: TurnEnvelope) -> list[MemoryMutation]: ...


class MemoryReviewer:
    """后台 reviewer：消费 outbox，把 turn 提炼为长期记忆（经 Manager.apply）。"""

    def __init__(
        self,
        store,
        manager,
        extractor: MemoryExtractor,
        *,
        clock=time.time,
        max_attempts: int = 3,
        lease_seconds: float = 30.0,
        backoff_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._manager = manager
        self._extractor = extractor
        self._clock = clock
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._backoff_seconds = backoff_seconds

    def process_pending(self) -> int:
        """处理所有当前可领取的 pending job；返回处理数量。"""
        count = 0
        while True:
            claimed = self._store.claim_review_job(
                worker_id="reviewer",
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
            if claimed is None:
                break
            job, turn = claimed
            self._process(job, turn)
            count += 1
        return count

    def _process(self, job, turn: TurnEnvelope) -> None:
        try:
            mutations = self._extractor.extract(turn)
            for mutation in mutations:
                self._manager.apply(mutation)
            self._store.complete_review_job(job.turn_id)
        except Exception as exc:
            self._store.fail_review_job(
                job.turn_id,
                repr(exc),
                next_available_at=self._clock() + self._backoff_seconds,
                max_attempts=self._max_attempts,
            )
