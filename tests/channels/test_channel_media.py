"""三端附件归一化：只用平台载荷与本地假下载，不接外网。"""
from __future__ import annotations

import io
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.channel import media
from agent.channel.channel_feishu import FeishuChannel
from agent.channel.channel_qq import QQChannel
from agent.channel.channel_wechat import WechatChannel
from agent.core.building_messages import build_user_message
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.model.media_messages import prepare_model_messages


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_ROOT", tmp_path / "media")


@pytest.mark.parametrize(
    ("message_type", "resource", "filename", "mime"),
    [
        ("image", {"image_key": "img_1"}, "picture.png", "image/png"),
        ("file", {"file_key": "file_1", "file_name": "report.pdf"}, "report.pdf", "application/pdf"),
    ],
)
def test_feishu_resource_becomes_content_part(message_type, resource, filename, mime):
    channel = FeishuChannel()
    response = SimpleNamespace(
        success=lambda: True,
        file=io.BytesIO(b"platform bytes"),
        file_name=filename,
        raw=SimpleNamespace(headers={"Content-Type": mime}),
    )
    channel._client = MagicMock()
    channel._client.im.v1.message_resource.get.return_value = response
    event = channel.parse_to_InboundEvent({
        "message_id": "om_1", "chat_id": "oc_1", "chat_type": "dm",
        "user_id": "ou_1", "text": "", "message_type": message_type,
        "resource": resource, "mentions": [],
    })
    assert len(event.content) == 1
    part = event.content[0]
    assert part.type == message_type
    assert part.file_name == filename
    assert part.mime_type == mime
    assert part.size_bytes == len(b"platform bytes")
    assert open(part.path, "rb").read() == b"platform bytes"
    assert part.path in [p.path for p in build_user_message(event).content]


@pytest.mark.parametrize(
    ("mime", "expected_type"),
    [("image/jpeg", "image"), ("application/pdf", "file")],
)
def test_qq_attachment_becomes_content_part(monkeypatch, mime, expected_type):
    monkeypatch.setattr("agent.channel.channel_qq.download_media", lambda *a, **k: b"qq bytes")
    channel = QQChannel()
    event = channel.parse_to_InboundEvent({
        "message_id": "m1", "chat_id": "u1", "chat_type": "dm", "user_id": "u1",
        "text": "请看", "attachments": [{
            "url": "https://example.qq.com/a", "filename": "a.jpg" if expected_type == "image" else "a.pdf",
            "content_type": mime,
        }],
    })
    assert event.text == "请看"
    assert event.content[0].type == expected_type
    assert event.content[0].mime_type == mime


@pytest.mark.parametrize(
    ("item_type", "detail_key", "name", "expected_type"),
    [(2, "image_item", "image.jpg", "image"), (4, "file_item", "report.pdf", "file")],
)
def test_wechat_decrypts_media(monkeypatch, item_type, detail_key, name, expected_type):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes(range(16))
    plain = b"wechat bytes"
    padded = plain + bytes([16 - len(plain) % 16]) * (16 - len(plain) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    monkeypatch.setattr("agent.channel.channel_wechat.download_media", lambda *a, **k: encrypted)
    channel = WechatChannel()
    event = channel.parse_to_InboundEvent({
        "message_id": "wx1", "chat_id": "peer", "chat_type": "dm", "user_id": "peer",
        "text": "", "media_items": [{"type": item_type, detail_key: {
            "file_name": name, "aeskey": key.hex(),
            "media": {"encrypt_query_param": "ticket"},
        }}],
    })
    part = event.content[0]
    assert part.type == expected_type
    assert open(part.path, "rb").read() == plain
    assert part.file_name == name


def test_wechat_multiple_images_and_file_keep_order_and_unique_paths(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.channel.channel_wechat.download_media", lambda *a, **k: b"media")
    channel = WechatChannel()
    event = channel.parse_to_InboundEvent({
        "message_id": "wx-many", "chat_id": "peer", "chat_type": "dm", "user_id": "peer",
        "text": "看看这些", "media_items": [
            {"type": 2, "image_item": {"media": {"full_url": "https://a.weixin.qq.com/1"}}},
            {"type": 2, "image_item": {"media": {"full_url": "https://a.weixin.qq.com/2"}}},
            {"type": 4, "file_item": {"file_name": "report.pdf", "media": {"full_url": "https://a.weixin.qq.com/3"}}},
        ],
    })
    assert [part.type for part in event.content] == ["image", "image", "file"]
    assert [part.file_name for part in event.content] == ["image-1.jpg", "image-2.jpg", "report.pdf"]
    assert len({part.path for part in event.content}) == 3
    mapped = PerceptionBuilder._map_message(build_user_message(event))
    prepared = prepare_model_messages([mapped], supports_images=True)[0]
    assert [block["type"] for block in prepared["content"]] == ["text", "image_url", "image_url"]
    assert "report.pdf" in prepared["content"][0]["text"]


def test_qq_multiple_attachments_get_distinct_fallback_names(monkeypatch):
    monkeypatch.setattr("agent.channel.channel_qq.download_media", lambda *a, **k: b"media")
    channel = QQChannel()
    event = channel.parse_to_InboundEvent({
        "message_id": "qq-many", "chat_id": "peer", "chat_type": "dm", "user_id": "peer",
        "text": "", "attachments": [
            {"url": "https://a.qq.com/1", "content_type": "image/jpeg"},
            {"url": "https://a.qq.com/2", "content_type": "image/jpeg"},
        ],
    })
    assert [part.type for part in event.content] == ["image", "image"]
    assert [part.file_name for part in event.content] == ["attachment-1.jpg", "attachment-2.jpg"]
    assert len({part.path for part in event.content}) == 2


def test_download_failure_is_visible_in_event(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("附件超过 20 MiB 限制")

    monkeypatch.setattr("agent.channel.channel_qq.download_media", fail)
    channel = QQChannel()
    event = channel.parse_to_InboundEvent({
        "message_id": "m1", "chat_id": "u1", "chat_type": "dm", "user_id": "u1",
        "text": "", "attachments": [{"url": "https://example.qq.com/a", "filename": "big.pdf"}],
    })
    assert event.content == []
    assert "附件读取失败" in event.text
    assert "20 MiB" in event.text
    assert channel.status["error_count"] == 1


def test_media_store_rejects_traversal_and_oversize():
    part = media.save_media_bytes(b"a", channel="qq", file_name="../../secret.txt", mime_type="text/plain")
    assert part.file_name == "secret.txt"
    with pytest.raises(ValueError, match="20 MiB"):
        media.save_media_bytes(b"x" * (media.MAX_MEDIA_BYTES + 1), channel="qq", file_name="big", mime_type="application/octet-stream")


def test_qq_media_callback_is_queued_and_duplicate_dropped(monkeypatch):
    class Gateway:
        def __init__(self):
            self.events = []

        def submit(self, event):
            self.events.append(event)

    monkeypatch.setattr("agent.channel.channel_qq.download_media", lambda *a, **k: b"image")
    gateway = Gateway()
    channel = QQChannel()
    channel.bind_gateway(gateway)
    raw = {
        "message_id": "queued-1", "chat_id": "u1", "chat_type": "dm", "user_id": "u1",
        "text": "", "attachments": [{
            "url": "https://multimedia.nt.qq.com.cn/img", "filename": "a.jpg", "content_type": "image/jpeg",
        }],
    }
    try:
        channel.ingest(raw)
        channel.ingest(raw)
        deadline = time.monotonic() + 2
        while len(gateway.events) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(gateway.events) == 1
        assert gateway.events[0].content[0].type == "image"
    finally:
        channel.stop()
