"""待处理作业消费者：连接事实队列、纯审查器与原子提交器。"""
# 职责：后台认领固定来源的提炼作业，加载轮次，续租、重试并提交结果。
# 边界：不决定提炼内容或卡片冲突规则；作业状态保存在 SQLite 队列中。
# 关联：由 main.py 启停，调用 MemoryBatchReviewer 与 memory_review_committer.py。
from __future__ import annotations

import logging
import threading
import time

from .memory_batch_reviewer import MemoryBatchReviewer
from .types.review_worker_types import MemoryReviewCommitter, MemoryReviewQueue


logger = logging.getLogger(__name__)

### 消费由 AgentLoop 产生的 outbox 作业，审查后交给 Committer 入库
#### 心跳循环领取作业；内部串联 Reviewer、Validator 与 Committer
class MemoryReviewWorker:
    """消费 outbox；同一 Interface 同时支持确定性测试与后台生命周期。"""

    def __init__(
        self,
        queue: MemoryReviewQueue,
        reviewer: MemoryBatchReviewer,
        committer: MemoryReviewCommitter,
        *,
        extractor_version: str,
        worker_id: str = "memory-reviewer",
        lease_seconds: float = 30.0,
        max_attempts: int = 3,
        backoff_seconds: float = 60.0,
        poll_interval: float = 0.5,
        shutdown_grace: float = 5.0,
        clock=time.time,
    ) -> None:
        self._queue = queue
        self._reviewer = reviewer
        self._committer = committer
        self._extractor_version = extractor_version
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._poll_interval = poll_interval
        self._shutdown_grace = shutdown_grace
        self._clock = clock
        self._wakeup = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="memory-reviewer",
            daemon=True,
        )
        self._thread.start()

    def notify(self, *_args, **_kwargs) -> None:
        self._wakeup.set()

    def stop(self, timeout: float | None = None) -> None:
        self._running = False
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout if timeout is not None else self._shutdown_grace)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                self.process_pending()
            except Exception:
                logger.exception("Memory review worker 未预期失败")
            self._wakeup.wait(self._poll_interval)
            self._wakeup.clear()

    def process_pending(self) -> int:
        processed = 0
        while True:
            job = self._queue.jobs.claim(
                job_type="memory.extract",
                handler_version=self._extractor_version,
                worker_id=self._worker_id,
                now=int(self._clock()*1000),
                lease_ms=int(self._lease_seconds*1000),
            )
            if job is None:
                return processed
            stopped = threading.Event()
            def heartbeat():
                while not stopped.wait(max(0.01,self._lease_seconds/3)):
                    try:
                        self._queue.jobs.renew(job, lease_ms=int(self._lease_seconds*1000),
                                               now=int(self._clock()*1000))
                    except Exception:
                        logger.exception("Memory job heartbeat failed")
                        return
            heartbeat_thread=threading.Thread(target=heartbeat,daemon=True)
            heartbeat_thread.start()
            try:
                # 传入本次需要处理的turn ids
                turn_ids = tuple(job.input.get("turn_ids") or (job.turn_id,))
                turns = tuple(self._queue.executions.get_record(identity) for identity in turn_ids)
                sessions = tuple(
                    self._queue.get_session(turn.session_id) if turn is not None else None
                    for turn in turns
                )
                if any(turn is None for turn in turns) or any(session is None for session in sessions):
                    raise LookupError(f"review job {job.id} source missing")
                result = self._reviewer.review_batch(turns, sessions)
                self._committer.commit(job, result, extractor_version=self._extractor_version,
                                       now=int(self._clock()*1000))
            except Exception as exc:
                try:
                    self._queue.jobs.fail(job, repr(exc),
                        available_at=int((self._clock()+self._backoff_seconds)*1000),
                        max_attempts=self._max_attempts, now=int(self._clock()*1000))
                except Exception:
                    logger.exception("Review failure could not be recorded; claim may have expired")
            finally:
                stopped.set()
                heartbeat_thread.join()
            processed += 1
