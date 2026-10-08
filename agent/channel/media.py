"""三渠道共享的入站附件存储与受限下载。"""
from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..core.types import ContentPart
from ..provider import ROOT

MEDIA_ROOT = ROOT / "data" / "channel" / "media"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _safe_name(name: str, mime_type: str) -> str:
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[^\w.\-]", "_", base, flags=re.UNICODE).strip("._")[:100]
    if not base:
        base = "attachment" + (mimetypes.guess_extension(mime_type) or ".bin")
    return base


def save_media_bytes(data: bytes, *, channel: str, file_name: str, mime_type: str) -> ContentPart:
    if not data:
        raise ValueError("附件为空")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("附件超过 20 MiB 限制")
    mime = (mime_type or "application/octet-stream").split(";", 1)[0].lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    name = _safe_name(file_name, mime)
    directory = MEDIA_ROOT / channel
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}-{name}"
    path.write_bytes(data)
    return ContentPart(
        type="image" if mime in IMAGE_MIMES else "file",
        path=str(path.resolve()),
        file_name=name,
        mime_type=mime,
        size_bytes=len(data),
    )


def download_media(url: str, *, allowed_domains: tuple[str, ...]) -> bytes:
    """只读平台 CDN 的 HTTPS 地址；不跟随重定向，也不下载超限内容。"""
    import httpx

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == domain or host.endswith("." + domain) for domain in allowed_domains
    ):
        raise ValueError("附件地址不在平台 CDN 允许范围")
    with httpx.stream("GET", url, timeout=30.0, follow_redirects=False) as response:
        response.raise_for_status()
        if int(response.headers.get("content-length") or 0) > MAX_MEDIA_BYTES:
            raise ValueError("附件超过 20 MiB 限制")
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise ValueError("附件超过 20 MiB 限制")
            chunks.append(chunk)
    return b"".join(chunks)
