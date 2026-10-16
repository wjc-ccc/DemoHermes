"""CronScheduler：到点提交的事件仍归原用户，但每次都是新会话；邮件可静默。"""
import time

from agent.core.channel.baseChannel import BaseChannel
from agent.core.cron.jobs import CronJob, JobStore
from agent.core.cron.mail import Mail
from agent.core.cron.scheduler import CRON_PROMPT, CronScheduler
from agent.core.gateway.gateway import SILENT_REPLY, Gateway
from agent.core.gateway.session_key import build_session_key
from agent.core.memory.memory_scope import build_memory_scope
from agent.types.channel import SessionSource
from agent.types.conversation import ContentPart
from agent.types.model import ModelResponse
from tests.loop_fakes import make_loop

ALICE = SessionSource(channel="feishu", chat_id="oc_alice", chat_type="dm", user_id="ou_alice",
                      reply_to_message_id="om_old")
SCOPE = build_memory_scope(ALICE)
NOW = 1_800_000_000.0


class _Gateway:
    def __init__(self):
        self.events = []

    def submit(self, event):
        self.events.append(event)


class _Mailbox:
    def __init__(self, mails):
        self.mails = mails
        self.state_paths = []

    def fetch_new(self, state_path):
        self.state_paths.append(state_path)
        mails, self.mails = self.mails, []
        return mails


def _add(store, **over):
    return store.add(SCOPE, CronJob(**{"session_key": build_session_key(ALICE), "next_run": NOW, **over}))


def test_prompt_job_submits_fresh_session_for_same_user(tmp_path):
    store, gateway = JobStore(tmp_path), _Gateway()
    job = _add(store, text="提醒我喝水", every_minutes=10)
    scheduler = CronScheduler(gateway, store)

    scheduler.tick(NOW)
    scheduler.tick(NOW + 600)

    first, second = gateway.events
    assert first.text == "提醒我喝水" and first.channel_prompt == CRON_PROMPT
    assert first.source.channel == "feishu" and first.source.chat_id == "oc_alice"
    assert first.source.reply_to_message_id is None
    assert first.source.thread_id.startswith(f"cron-{job.job_id}-")
    assert build_memory_scope(first.source).storage_key == SCOPE.storage_key
    keys = {build_session_key(first.source), build_session_key(second.source), build_session_key(ALICE)}
    assert len(keys) == 3
    assert store.list(SCOPE)[0].next_run == NOW + 1200


def test_one_shot_job_is_removed_after_firing(tmp_path):
    store, gateway = JobStore(tmp_path), _Gateway()
    _add(store, text="开会", at=NOW)
    CronScheduler(gateway, store).tick(NOW)
    assert len(gateway.events) == 1
    assert store.list(SCOPE) == []


def test_mail_job_submits_one_event_per_mail(tmp_path):
    store, gateway = JobStore(tmp_path), _Gateway()
    _add(store, kind="mail", text="只提醒导师的邮件", every_minutes=5)
    image = ContentPart(type="image", path="x.png", file_name="x.png", mime_type="image/png")
    mailbox = _Mailbox([
        Mail(uid=7, subject="周报", sender="boss@163.com", date="d", body="请交周报", attachments=[image]),
        Mail(uid=8, subject="广告", sender="ad@x.com", date="d", body="促销"),
    ])

    CronScheduler(gateway, store, mailbox=mailbox).tick(NOW)

    assert [event.source.thread_id.split("-")[1] for event in gateway.events] == ["7", "8"]
    assert "周报" in gateway.events[0].text and gateway.events[0].content == [image]
    assert SILENT_REPLY in gateway.events[0].channel_prompt
    assert "只提醒导师的邮件" in gateway.events[0].channel_prompt
    assert mailbox.state_paths == [tmp_path / SCOPE.storage_key / "mail_state.json"]


def test_mail_job_without_mailbox_is_skipped(tmp_path):
    store, gateway = JobStore(tmp_path), _Gateway()
    _add(store, kind="mail", every_minutes=5)
    CronScheduler(gateway, store, mailbox=None).tick(NOW)
    assert gateway.events == []
    assert store.list(SCOPE)[0].next_run == NOW + 300


# ---- 经过真实 Gateway：推送回原 channel，静默标记不投递 ----

class _Model:
    name = "mock"

    def __init__(self, texts):
        self.texts = list(texts)

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text=self.texts.pop(0))


class _FeishuChannel(BaseChannel):
    name = "feishu"

    def __init__(self):
        super().__init__()
        self.delivered = []

    def parse_to_InboundEvent(self, raw):
        raise NotImplementedError

    def deliver_to_OutboundEvent(self, reply):
        self.delivered.append(reply)

    def start(self):
        pass


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_reminder_and_silent_mail(tmp_path, session_store):
    gateway = Gateway(loop=make_loop(model=_Model(["该喝水了", SILENT_REPLY])), store=session_store, workers=1)
    channel = _FeishuChannel()
    gateway.register_channel(channel)
    gateway.start()
    try:
        store = JobStore(tmp_path)
        _add(store, text="提醒我喝水", at=NOW)
        _add(store, kind="mail", every_minutes=5)
        mailbox = _Mailbox([Mail(uid=1, subject="广告", sender="ad@x.com", date="d", body="促销")])
        CronScheduler(gateway, store, mailbox=mailbox).tick(NOW)

        assert _wait(lambda: len(session_store.list_sessions()) >= 2)
        assert _wait(lambda: len(channel.delivered) >= 1)
        time.sleep(0.3)
        assert [reply.text for reply in channel.delivered] == ["该喝水了"]
        assert channel.delivered[0].source.chat_id == "oc_alice"
        assert channel.delivered[0].source.reply_to_message_id is None
        assert session_store.find(build_session_key(ALICE)) is None
    finally:
        gateway.stop()
