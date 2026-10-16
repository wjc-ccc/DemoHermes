"""NeteaseMailbox：先发 ID 再只读选箱，按 UID 增量拉取，首次与 uidvalidity 变化时不翻旧邮件。"""
import re
from email.message import EmailMessage

import pytest

from agent.core.channel import media
from agent.core.cron.mail import NeteaseMailbox

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _mail(subject: str, *, image: bool = False) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "导师 <boss@163.com>"
    message["Date"] = "Fri, 16 Oct 2026 10:00:00 +0800"
    message.set_content(f"{subject} 的正文")
    if image:
        message.add_attachment(PNG, maintype="image", subtype="png", filename="chart.png")
    return message.as_bytes()


class FakeImap:
    """模拟 163：未发 ID 时拒绝 SELECT；UID n:* 无新邮件时仍返回最大 UID。"""

    def __init__(self, mails: dict[int, bytes], uidvalidity: bytes = b"100"):
        self.mails = mails
        self.uidvalidity = uidvalidity
        self.id_sent = False
        self.readonly = None
        self.fetch_items = []
        self.logged_out = False

    def __call__(self, host):
        assert host == "imap.163.com"
        self.id_sent = False
        return self

    def login(self, user, code):
        assert (user, code) == ("me@163.com", "auth")

    def xatom(self, name, arg):
        assert name == "ID"
        self.id_sent = True
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        if not self.id_sent:
            return "NO", [b"Unsafe Login"]
        self.readonly = readonly
        return "OK", [str(len(self.mails)).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [self.uidvalidity]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[-1]
            uids = sorted(self.mails)
            if criteria != "ALL":
                start = int(re.fullmatch(r"UID (\d+):\*", criteria).group(1))
                uids = [uid for uid in uids if uid >= start] or uids[-1:]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            uid, items = int(args[0]), args[1]
            self.fetch_items.append(items)
            return "OK", [(f"1 (UID {uid} BODY[] {{1}}".encode(), self.mails[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_ROOT", tmp_path / "media")


def test_incremental_fetch(tmp_path):
    imap = FakeImap({1: _mail("旧邮件一"), 2: _mail("旧邮件二")})
    mailbox = NeteaseMailbox("me@163.com", "auth", connect=imap)
    state = tmp_path / "mail_state.json"

    assert mailbox.fetch_new(state) == []
    assert imap.readonly is True and imap.logged_out

    imap.mails[3] = _mail("新邮件", image=True)
    mails = mailbox.fetch_new(state)
    assert [mail.uid for mail in mails] == [3]
    assert mails[0].subject == "新邮件" and "boss@163.com" in mails[0].sender
    assert "新邮件 的正文" in mails[0].body
    assert [part.type for part in mails[0].attachments] == ["image"]
    assert imap.fetch_items == ["(BODY.PEEK[])"]

    assert mailbox.fetch_new(state) == []


def test_uidvalidity_change_resets_without_flood(tmp_path):
    imap = FakeImap({1: _mail("a")})
    mailbox = NeteaseMailbox("me@163.com", "auth", connect=imap)
    state = tmp_path / "mail_state.json"
    mailbox.fetch_new(state)

    imap.mails = {5: _mail("b"), 6: _mail("c")}
    imap.uidvalidity = b"200"
    assert mailbox.fetch_new(state) == []
    imap.mails[7] = _mail("d")
    assert [mail.uid for mail in mailbox.fetch_new(state)] == [7]


def test_from_env_requires_both_credentials(monkeypatch):
    monkeypatch.setenv("NETEASE_MAIL_USER", "me@163.com")
    monkeypatch.delenv("NETEASE_MAIL_AUTH_CODE", raising=False)
    assert NeteaseMailbox.from_env() is None
    monkeypatch.setenv("NETEASE_MAIL_AUTH_CODE", "auth")
    assert NeteaseMailbox.from_env().host == "imap.163.com"
