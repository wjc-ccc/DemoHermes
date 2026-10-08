"""附件只在视觉模型请求时转为图像块，持久化消息保留本地引用。"""
from __future__ import annotations

from agent.channel import media
from agent.core.building_messages import build_user_message
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.core.types import InboundEvent, SessionSource
from agent.model.claude import ClaudeModel
from agent.model.media_messages import prepare_model_messages


def test_image_model_mapping_and_text_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr("agent.model.media_messages.MEDIA_ROOT", tmp_path / "media")
    part = media.save_media_bytes(b"image bytes", channel="qq", file_name="x.png", mime_type="image/png")
    event = InboundEvent(text="这是什么？", content=[part], source=SessionSource(channel="qq", chat_id="u"))
    message = build_user_message(event)
    mapped = PrecisionBuilder._map_message(message)
    assert mapped["_images"][0]["path"] == part.path
    assert "内容未解析" in mapped["content"]
    assert "_images" not in prepare_model_messages([mapped], supports_images=False)[0]

    prepared = prepare_model_messages([mapped], supports_images=True)[0]
    assert prepared["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    _, anthropic = ClaudeModel._convert_messages([prepared])
    assert anthropic[0]["content"][1]["type"] == "image"
    assert anthropic[0]["content"][1]["source"]["media_type"] == "image/png"


def test_custom_anthropic_endpoint_needs_explicit_image_capability(monkeypatch):
    monkeypatch.setattr("agent.model.claude.ANTHROPIC_BASE_URL", "https://ark.example.com")
    monkeypatch.delenv("MODEL_SUPPORTS_IMAGES", raising=False)
    assert ClaudeModel().supports_images is False
    monkeypatch.setenv("MODEL_SUPPORTS_IMAGES", "true")
    assert ClaudeModel().supports_images is True
