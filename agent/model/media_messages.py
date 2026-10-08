"""把持久化的媒体引用转换成模型请求，避免在 Session 中保存 base64。"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..provider import ROOT

logger = logging.getLogger(__name__)
MEDIA_ROOT = ROOT / "data" / "channel" / "media"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def prepare_model_messages(messages: list[dict], *, supports_images: bool) -> list[dict]:
    """剥离内部媒体引用；视觉模型在请求前才读取已缓存图片。"""
    prepared: list[dict] = []
    media_root = MEDIA_ROOT.resolve()
    for message in messages:
        item = {key: value for key, value in message.items() if key != "_images"}
        images = message.get("_images") or []
        if not images or not supports_images:
            prepared.append(item)
            continue
        blocks: list[dict] = [{
            "type": "text",
            "text": str(item.get("content") or "").replace("；内容未解析]", "]"),
        }]
        for image in images:
            try:
                path = Path(str(image["path"])).resolve(strict=True)
                if not path.is_relative_to(media_root):
                    raise ValueError("image path outside media root")
                mime = str(image.get("mime_type") or "")
                if mime not in IMAGE_MIMES:
                    raise ValueError("unsupported image MIME")
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError("image exceeds size limit")
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                })
            except (OSError, KeyError, ValueError) as exc:
                logger.warning("模型图片读取失败 file=%s err=%s", image.get("file_name"), exc)
                blocks[0]["text"] += f"\n[图片读取失败: {image.get('file_name') or 'image'}]"
        item["content"] = blocks if len(blocks) > 1 else blocks[0]["text"]
        prepared.append(item)
    return prepared
