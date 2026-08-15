"""TurnReviewer - 自动后台提炼。

从 SessionStore outbox 原子 claim pending review job，调用注入的 TurnExtractor
产出 BuiltinMemoryRequest，经 Manager.apply 落库；成功标 done，失败有界退避回 pending，
超 max_attempts 标 failed。Reviewer 不直接写文件，只调 Manager。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from .types import BuiltinMemoryRequest, TurnRecord

logger = logging.getLogger(__name__)


class MemoryExtractor(Protocol):
    """抽取 seam：TurnRecord → 零个或多个 BuiltinMemoryRequest。"""

    def extract(self, turn: TurnRecord) -> list[BuiltinMemoryRequest]: ...


class TurnReviewer:
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
        worker_id: str = "reviewer",
    ) -> None:
        self._store = store
        self._manager = manager
        self._extractor = extractor
        self._clock = clock
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._backoff_seconds = backoff_seconds
        self._worker_id = worker_id

    def process_pending(self) -> int:
        """处理所有当前可领取的 pending job；返回处理数量。"""
        count = 0
        while True:
            claimed = self._store.claim_review_job(
                worker_id=self._worker_id,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
            if claimed is None:
                break
            job, turn = claimed
            self._process(job, turn)
            count += 1
        return count

    def _process(self, job, turn: TurnRecord) -> None:
        try:
            for request in self._extractor.extract(turn):
                self._manager.apply(request)
            self._store.complete_review_job(job.turn_id)
        except Exception as exc:
            self._store.fail_review_job(
                job.turn_id,
                repr(exc),
                next_available_at=self._clock() + self._backoff_seconds,
                max_attempts=self._max_attempts,
            )


class ReviewerWorker:
    """后台线程：轮询 / 被 notify 唤醒后消费 review outbox（§8.1 / §8.3）。

    不阻断主对话；shutdown 时停止 claim 新 job，并给正在处理的有限 grace。
    """

    def __init__(
        self,
        reviewer: TurnReviewer,
        *,
        poll_interval: float = 0.5,
        shutdown_grace: float = 5.0,
    ) -> None:
        self._reviewer = reviewer
        self._poll_interval = poll_interval
        self._shutdown_grace = shutdown_grace
        self._wakeup = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="turn-reviewer", daemon=True,
        )
        self._thread.start()
        logger.info("TurnReviewer worker 已启动")

    def notify(self, *_args, **_kwargs) -> None:
        """commit_turn / session_end 后唤醒；忽略多余参数便于当 hook 用。"""
        self._wakeup.set()

    def stop(self, timeout: float | None = None) -> None:
        self._running = False
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout if timeout is not None else self._shutdown_grace)
            self._thread = None
        try:
            n = self._reviewer.process_pending()
            if n:
                logger.info("shutdown 时额外处理了 %d 个 review job", n)
        except Exception:
            logger.exception("shutdown review drain 失败")
        logger.info("TurnReviewer worker 已停止")

    def _loop(self) -> None:
        while self._running:
            try:
                self._reviewer.process_pending()
            except Exception:
                logger.exception("reviewer process_pending 异常（已吞掉，不阻断主对话）")
            self._wakeup.wait(timeout=self._poll_interval)
            self._wakeup.clear()
