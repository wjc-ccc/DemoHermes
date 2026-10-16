"""
NeteaseMailbox — 网易邮箱 IMAP 增量拉取（标准库 imaplib，不依赖 MCP）

配置（.env，不经过对话）：
    NETEASE_MAIL_USER       邮箱地址
    NETEASE_MAIL_AUTH_CODE  客户端授权码（不是登录密码）
    NETEASE_IMAP_HOST       可选，默认 imap.163.com
163 登录后必须先发 IMAP ID 命令，否则 SELECT 会被拒为 Unsafe Login。

增量位置存在用户目录的 mail_state.json（uidvalidity + last_uid）：
    首次运行或 uidvalidity 变化 → 只记录当前最大 UID，不把旧邮件全部拉起来
    之后只取更大的 UID；EXAMINE + BODY.PEEK 不把邮件标成已读
"""
from __future__ import annotations

import email
import html
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from email import policy
from pathlib import Path

from agent.core.channel.media import save_media_bytes
from agent.types.conversation import ContentPart

logger = logging.getLogger(__name__)

MAIL_STATE_FILE = "mail_state.json"
MAX_BODY_CHARS = 4000
MAX_MAILS_PER_POLL = 20
_IMAP_ID = '("name" "personal-agent" "version" "1.0" "vendor" "personal-agent")'


@dataclass
class Mail:
    uid: int
    subject: str
    sender: str
    date: str
    body: str
    attachments: list[ContentPart] = field(default_factory=list)

    def render(self) -> str:
        return f"新邮件\n主题：{self.subject}\n发件人：{self.sender}\n时间：{self.date}\n\n{self.body}"


class NeteaseMailbox:
    def __init__(self, user: str, auth_code: str, *, host: str = "imap.163.com", connect=imaplib.IMAP4_SSL) -> None:
        self.user = user
        self.auth_code = auth_code
        self.host = host
        self._connect = connect

    @classmethod
    def from_env(cls) -> NeteaseMailbox | None:
        user = os.getenv("NETEASE_MAIL_USER", "").strip()
        code = os.getenv("NETEASE_MAIL_AUTH_CODE", "").strip()
        if not user or not code:
            return None
        return cls(user, code, host=os.getenv("NETEASE_IMAP_HOST", "").strip() or "imap.163.com")

    def fetch_new(self, state_path: Path) -> list[Mail]:
        state = _read_state(state_path)
        conn = self._connect(self.host)
        try:
            conn.login(self.user, self.auth_code)
            conn.xatom("ID", _IMAP_ID)
            typ, data = conn.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError(f"选择收件箱失败: {data!r}")
            uidvalidity = _uidvalidity(conn)
            if state.get("uidvalidity") != uidvalidity:
                _write_state(state_path, uidvalidity, max(_search(conn, "ALL"), default=0))
                return []
            last_uid = int(state.get("last_uid", 0))
            # UID n:* 在没有新邮件时仍会返回当前最大 UID，需要再过滤一次
            new_uids = sorted(uid for uid in _search(conn, f"UID {last_uid + 1}:*") if uid > last_uid)
            new_uids = new_uids[:MAX_MAILS_PER_POLL]
            mails = []
            for uid in new_uids:
                try:
                    mails.append(_parse(uid, _fetch(conn, uid)))
                except Exception:
                    logger.exception("邮件解析失败，已跳过 uid=%s", uid)
            if new_uids:
                _write_state(state_path, uidvalidity, new_uids[-1])
            return mails
        finally:
            try:
                conn.logout()
            except Exception:
                logger.debug("IMAP logout 失败", exc_info=True)


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(path: Path, uidvalidity: str, last_uid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"uidvalidity": uidvalidity, "last_uid": last_uid}), encoding="utf-8")
    tmp.replace(path)


def _uidvalidity(conn) -> str:
    _, data = conn.response("UIDVALIDITY")
    value = data[0] if data else None
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _search(conn, criteria: str) -> list[int]:
    typ, data = conn.uid("SEARCH", None, criteria)
    if typ != "OK":
        raise RuntimeError(f"IMAP SEARCH 失败: {data!r}")
    return [int(uid) for uid in (data[0] or b"").split()]


def _fetch(conn, uid: int) -> bytes:
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"IMAP FETCH 失败: {data!r}")
    return next(item[1] for item in data if isinstance(item, tuple))


def _parse(uid: int, raw: bytes) -> Mail:
    message = email.message_from_bytes(raw, policy=policy.default)
    body = ""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()[:MAX_BODY_CHARS]
    attachments: list[ContentPart] = []
    for item in message.iter_attachments():
        name = item.get_filename() or ""
        try:
            attachments.append(save_media_bytes(
                item.get_payload(decode=True) or b"", channel="mail",
                file_name=name, mime_type=item.get_content_type(),
            ))
        except ValueError as exc:
            body += f"\n[附件读取失败: {name or 'attachment'}：{exc}]"
    return Mail(
        uid=uid,
        subject=str(message.get("subject") or ""),
        sender=str(message.get("from") or ""),
        date=str(message.get("date") or ""),
        body=body,
        attachments=attachments,
    )
