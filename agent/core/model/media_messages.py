"""把持久化的媒体引用转换成模型请求，避免在 Session 中保存 base64。"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..channel import media as channel_media

logger = logging.getLogger(__name__)


def prepare_model_messages(messages: list[dict], *, supports_images: bool) -> list[dict]:
    """剥离内部媒体引用；视觉模型在请求前才读取已缓存图片（只认 channel 落盘目录）。"""
    prepared: list[dict] = []
    media_root = channel_media.MEDIA_ROOT.resolve()
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
                if mime not in channel_media.IMAGE_MIMES:
                    raise ValueError("unsupported image MIME")
                if path.stat().st_size > channel_media.MAX_MEDIA_BYTES:
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
