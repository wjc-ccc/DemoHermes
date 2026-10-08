"""普通 Markdown 手册的整文档读写。

不识别标题协议、隐藏 marker 或条目 ID。复杂度集中在原子替换、并发修订检查、
内容哈希和 token 统计，调用方只需 read / replace 两个操作。
"""
# 职责：把一份 Markdown 文件读成带指纹的快照，并按预期版本原子替换。
# 边界：不解释标题或内容含义，也不决定哪些事实应进入用户卡片。
# 关联：MemoryContextLoader 读取 Policy/Project；MemoryManualEditor 管理用户手册。
from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .memory_item import content_sha256
from .memory_layer import MemoryLayer, validate_item_type
from .memory_scope import MemoryScope


class MarkdownRevisionConflictError(Exception):
    error_code = "markdown_revision_conflict"


class MarkdownBookReadOnlyError(Exception):
    error_code = "markdown_book_read_only"

## 转化为markdown快照 
class MarkdownBookSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    layer: MemoryLayer
    type: str
    scope: MemoryScope
    content: str
    content_hash: str
    token_count: int


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def estimate_markdown_tokens(content: str) -> int:
    """统一估算 Markdown 成本；中英文混排时按 UTF-8 字节保守计数。"""
    if not content:
        return 0
    return max(len(content.split()), (len(content.encode("utf-8")) + 2) // 3)


_default_token_counter = estimate_markdown_tokens

## 一个markdown的文件管理类
### 负责整文件 加载和替换 以及对应的快照
class MarkdownBook:
    """一份 Markdown 的深模块：整文档快照 + 乐观并发原子替换。"""

    def __init__(
        self,
        path: str | Path,
        *,
        layer: MemoryLayer,
        type: str,
        scope: MemoryScope | None = None,
        writable: bool = False,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        """一个markdown记忆的文件管理类别 要包含layer path 是否可修改 以及读 替换 快照的逻辑 用于记忆的拼装"""
        validate_item_type(layer, type)
        self._path = Path(path)
        self._layer = layer
        self._type = type
        self._scope = scope or MemoryScope()
        self._writable = writable
        self._token_counter = token_counter or _default_token_counter

    @property
    def path(self) -> Path:
        return self._path
    
    ## markdown的读取 并且通过lock锁住对应的文件
    def read(self) -> MarkdownBookSnapshot:
        with _lock_for(self._path):
            content = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
            return self._snapshot(content)

    def replace(
        self, content: str, *, expected_hash: str | None = None
    ) -> MarkdownBookSnapshot:
        if not self._writable:
            raise MarkdownBookReadOnlyError(str(self._path))
        with _lock_for(self._path):
            current = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
            current_hash = content_sha256(current)
            if expected_hash is not None and expected_hash != current_hash:
                raise MarkdownRevisionConflictError(
                    f"expected={expected_hash} actual={current_hash}"
                )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self._path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                raise
            return self._snapshot(content)

    def _snapshot(self, content: str) -> MarkdownBookSnapshot:
        return MarkdownBookSnapshot(
            path=str(self._path),
            layer=self._layer,
            type=self._type,
            scope=self._scope,
            content=content,
            content_hash=content_sha256(content),
            token_count=max(0, int(self._token_counter(content))),
        )
