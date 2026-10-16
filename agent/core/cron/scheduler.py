"""
CronScheduler — 到点后用登记时的会话来源提交一轮新会话

每次触发复制原 SessionSource，只换唯一 thread_id、清空 reply_to_message_id：
    session_key 带上新 thread → 新会话，没有历史
    build_memory_scope 不看 thread → 仍归原用户
    出站按 source.channel → 回到原 channel
Gateway 不识别定时任务或邮件，只看到普通 InboundEvent。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from agent.core.gateway.gateway import SILENT_REPLY
from agent.core.gateway.session_key import parse_session_key
from agent.types.channel import InboundEvent, SessionSource
from .jobs import CronJob, JobStore
from .mail import MAIL_STATE_FILE

logger = logging.getLogger(__name__)

CRON_PROMPT = "这是定时任务触发的独立执行，没有先前对话。直接完成下面的任务，你的回复会作为提醒发给用户。"
MAIL_PROMPT = (
    "这是新邮件触发的独立执行，没有先前对话。判断这封邮件是否值得提醒用户："
    f"值得就用一两句话说明发件人、要点和需要做什么；不值得就只回复 {SILENT_REPLY}。"
)


class CronScheduler:
    def __init__(self, gateway, jobs: JobStore, *, mailbox=None, interval: float = 30.0, clock=time.time) -> None:
        self._gateway = gateway
        self._jobs = jobs
        self._mailbox = mailbox
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cron-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        # 首次扫描推迟一个周期，等各 channel 注册完再投递
        while not self._stop.wait(self._interval):
            self.tick()

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        for directory, job in self._jobs.due(now):
            try:
                self._fire(directory, job)
            except Exception:
                logger.exception("定时任务执行失败 job=%s dir=%s", job.job_id, directory.name)
            self._jobs.advance(directory, job.job_id, now)

    def _fire(self, directory: Path, job: CronJob) -> None:
        source = parse_session_key(job.session_key)
        if source is None:
            raise ValueError(f"session_key 无法还原: {job.session_key}")
        if job.kind == "prompt":
            self._submit(source, job.text, CRON_PROMPT, thread_id=f"cron-{job.job_id}-{uuid.uuid4().hex[:8]}")
            return
        if self._mailbox is None:
            logger.warning("未配置网易邮箱凭据，跳过邮件任务 job=%s", job.job_id)
            return
        prompt = MAIL_PROMPT + (f"\n用户对邮件提醒的要求：{job.text}" if job.text else "")
        for mail in self._mailbox.fetch_new(directory / MAIL_STATE_FILE):
            self._submit(source, mail.render(), prompt,
                         thread_id=f"mail-{mail.uid}-{uuid.uuid4().hex[:8]}", content=mail.attachments)

    def _submit(self, source: SessionSource, text: str, prompt: str, *, thread_id: str, content=()) -> None:
        self._gateway.submit(InboundEvent(
            text=text,
            content=list(content),
            source=source.model_copy(update={"thread_id": thread_id, "reply_to_message_id": None}),
            channel_prompt=prompt,
        ))
