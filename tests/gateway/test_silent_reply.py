"""回复正文为 SILENT_REPLY 时不投递 channel，但同步等待者照常拿到回复、该轮照常落库。"""
from agent.core.channel.baseChannel import BaseChannel
from agent.core.gateway.gateway import SILENT_REPLY, Gateway
from agent.core.gateway.session_key import build_session_key
from agent.types.channel import InboundEvent, SessionSource
from agent.types.model import ModelResponse
from tests.loop_fakes import make_loop


class _Model:
    name = "mock"

    def __init__(self, texts):
        self.texts = list(texts)

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text=self.texts.pop(0))


class _Channel(BaseChannel):
    name = "feishu"

    def __init__(self):
        super().__init__()
        self.delivered = []

    def parse_to_InboundEvent(self, raw):
        raise NotImplementedError

    def deliver_to_OutboundEvent(self, reply):
        self.delivered.append(reply.text)

    def start(self):
        pass


def test_silent_reply_skips_delivery_only(session_store):
    source = SessionSource(channel="feishu", chat_id="oc_1", chat_type="dm", user_id="ou_1")
    gateway = Gateway(loop=make_loop(model=_Model([f"  {SILENT_REPLY}\n", "正常回复"])),
                      store=session_store, workers=1)
    channel = _Channel()
    gateway.register_channel(channel)
    gateway.start()
    try:
        silent = gateway.ask(InboundEvent(text="新邮件", source=source), timeout=10)
        normal = gateway.ask(InboundEvent(text="你好", source=source), timeout=10)
    finally:
        gateway.stop()
    assert silent.text.strip() == SILENT_REPLY
    assert normal.text == "正常回复"
    assert channel.delivered == ["正常回复"]
    session = session_store.find(build_session_key(source))
    assert [m.content_text for m in session.messages if m.role == "assistant"][0].strip() == SILENT_REPLY
