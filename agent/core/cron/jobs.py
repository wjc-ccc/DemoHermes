"""按用户存放的定时任务。

文件：{root}/{memory_scope.storage_key}/jobs.json，与记忆、会话共用同一个 scope 归属。
时间三选一，均按本机本地时间：
    at            一次性（执行后删除）
    daily         每天 HH:MM
    every_minutes 每隔若干分钟
工具线程登记、调度线程推进共用同一把锁；写入先写临时文件再替换。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent.core.memory.memory_scope import ensure_scope_dir
from agent.types.memory.scope import MemoryScope

logger = logging.getLogger(__name__)

JOBS_FILE = "jobs.json"


class CronJob(BaseModel):
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    kind: Literal["prompt", "mail"] = "prompt"
    text: str = ""
    at: float | None = None
    daily: str | None = None
    every_minutes: int | None = None
    session_key: str  # 登记时所在会话；触发时据此还原推送来源
    next_run: float = 0.0
    created_at: float = Field(default_factory=time.time)


def next_daily(hhmm: str, now: float) -> float:
    """HH:MM 在 now 之后的下一次出现。"""
    hour, minute = (int(part) for part in hhmm.strip().split(":"))
    candidate = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def parse_at(raw: str, now: float) -> float:
    """HH:MM 取下一次出现；YYYY-MM-DD HH:MM 取指定时刻。"""
    text = raw.strip()
    if len(text) <= 5:
        return next_daily(text, now)
    return datetime.fromisoformat(text).timestamp()


def next_run_after(job: CronJob, now: float) -> float | None:
    """本次执行后的下一次时间；一次性任务返回 None。"""
    if job.daily:
        return next_daily(job.daily, now)
    if job.every_minutes:
        return now + job.every_minutes * 60
    return None


def _load(directory: Path) -> list[CronJob]:
    path = directory / JOBS_FILE
    if not path.exists():
        return []
    return [CronJob.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def _save(directory: Path, jobs: list[CronJob]) -> None:
    path = directory / JOBS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps([job.model_dump(mode="json") for job in jobs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def list(self, scope: MemoryScope) -> list[CronJob]:
        with self._lock:
            return _load(self.root / scope.storage_key)

    def add(self, scope: MemoryScope, job: CronJob) -> CronJob:
        with self._lock:
            directory = ensure_scope_dir(self.root, scope)
            _save(directory, [*_load(directory), job])
        return job

    def remove(self, scope: MemoryScope, job_id: str) -> bool:
        with self._lock:
            directory = self.root / scope.storage_key
            jobs = _load(directory)
            kept = [job for job in jobs if job.job_id != job_id]
            if len(kept) == len(jobs):
                return False
            _save(directory, kept)
            return True

    def due(self, now: float) -> list[tuple[Path, CronJob]]:
        """所有用户目录里到点的任务；单个文件损坏只跳过该用户。"""
        result: list[tuple[Path, CronJob]] = []
        with self._lock:
            for path in sorted(self.root.glob(f"*/{JOBS_FILE}")):
                try:
                    jobs = _load(path.parent)
                except (OSError, ValueError):
                    logger.exception("定时任务文件读取失败，已跳过: %s", path)
                    continue
                result.extend((path.parent, job) for job in jobs if job.next_run <= now)
        return result

    def advance(self, directory: Path, job_id: str, now: float) -> None:
        """推进下一次运行时间；一次性任务删除。"""
        with self._lock:
            kept: list[CronJob] = []
            for job in _load(directory):
                if job.job_id == job_id:
                    following = next_run_after(job, now)
                    if following is None:
                        continue
                    job.next_run = following
                kept.append(job)
            _save(directory, kept)
