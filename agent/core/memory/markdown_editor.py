"""受约束的个人 MEMORY.md 编辑、外部修改登记和版本恢复。"""
# 职责：校验用户手册的 scope、内容预算和版本，登记人工改动并支持恢复。
# 边界：只能编辑个人 MEMORY.md，不处理 Policy/Project 或结构化卡片冲突。
# 关联：memory_edit 工具与 MemoryContextLoader 调用；手册版本不生成卡片。
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .markdown_book import (
    MarkdownBookSnapshot,
    MarkdownRevisionConflictError,
    _lock_for,
    estimate_markdown_tokens,
)
from .memory_item import content_sha256
from .memory_layer import MemoryLayer
from .memory_scope import MemoryScope, ensure_scope_dir
from .memory_snapshot import MemoryDiagnostic

if TYPE_CHECKING:
    from ...storage.sqlite.database import Database


class MemoryManualError(ValueError):
    error_code = "memory_manual_error"


class MemoryManualScopeError(MemoryManualError):
    error_code = "memory_manual_scope_denied"


class MemoryManualContentError(MemoryManualError):
    error_code = "memory_manual_content_invalid"


class MemoryManualFileInvalidError(MemoryManualError):
    error_code = "memory_manual_file_invalid"


@dataclass(frozen=True)
class ManualRead:
    snapshot: MarkdownBookSnapshot
    diagnostics: tuple[MemoryDiagnostic, ...]
    # 真实磁盘内容的哈希；文件编码无效时仍可据此显式恢复。
    disk_hash: str
    revision: int | None = None
    external_change: bool = False


_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:password|passwd|api[_ -]?key|secret|access[_ -]?token|token|密码|密钥|访问令牌)\s*[:=：]\s*\S+"
)
_API_SECRET = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")
_IDENTITY_NUMBER = re.compile(r"\b\d{17}[\dXx]\b")
_INVALID_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryManualEditor:
    """每个用户只管理其 scope 目录内的普通 Markdown 手册。

    ``read`` 同步并登记人工改动。无效磁盘内容不会进入上下文，读取返回最近
    一份有效版本和诊断。写入要求调用方提交本次读取到的哈希，避免覆盖人工改动。
    """

    def __init__(
        self,
        root: str | Path,
        db: Database,
        *,
        max_tokens: int = 1200,
        token_counter: Callable[[str], int] = estimate_markdown_tokens,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self._root = Path(root).resolve()
        self._db = db
        self._max_tokens = max_tokens
        self._token_counter = token_counter

    def read(self, scope: MemoryScope) -> ManualRead:
        path = self._path(scope)
        with _lock_for(path):
            raw = path.read_bytes() if path.exists() else b""
            disk_hash = _bytes_hash(raw)
            with self._db.write() as conn:
                latest = _latest(conn, scope.storage_key)
                try:
                    content = raw.decode("utf-8")
                    # Path.read_text 会统一转换换行符；这里读取原始字节以识别人工文件变更。
                    content = content.replace("\r\n", "\n").replace("\r", "\n")
                    self._validate(content)
                except (UnicodeError, MemoryManualContentError) as exc:
                    fallback = latest[2] if latest is not None else ""
                    return ManualRead(
                        snapshot=self._snapshot(path, scope, fallback),
                        diagnostics=(
                            MemoryDiagnostic(
                                code="memory_document_invalid",
                                message=f"MEMORY.md: {exc}",
                            ),
                        ),
                        disk_hash=disk_hash,
                        revision=latest[0] if latest is not None else None,
                    )

                snapshot = self._snapshot(path, scope, content)
                changed = latest is None or latest[3] != snapshot.content_hash
                revision = (latest[0] + 1) if latest is not None else 1
                if changed:
                    _insert_version(
                        conn,
                        scope.storage_key,
                        revision,
                        snapshot,
                        actor="human" if path.exists() else "system",
                        reason="external_edit" if path.exists() else "bootstrap",
                        source_ref=None,
                    )
                return ManualRead(
                    snapshot=snapshot, diagnostics=(), disk_hash=disk_hash,
                    revision=revision if changed else latest[0],
                    external_change=changed and (latest is not None or path.exists()),
                )

    def peek(self, scope: MemoryScope) -> ManualRead:
        """只读预览；沿用校验与有效版本回退，但不登记外部文件变更。"""
        path = self._path(scope)
        with _lock_for(path):
            raw = path.read_bytes() if path.exists() else b""
            disk_hash = _bytes_hash(raw)
            with self._db.read() as conn:
                latest = _latest(conn, scope.storage_key)
            try:
                content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                self._validate(content)
            except (UnicodeError, MemoryManualContentError) as exc:
                fallback = latest[2] if latest is not None else ""
                return ManualRead(
                    snapshot=self._snapshot(path, scope, fallback),
                    diagnostics=(MemoryDiagnostic(
                        code="memory_document_invalid", message=f"MEMORY.md: {exc}"
                    ),),
                    disk_hash=disk_hash,
                    revision=latest[0] if latest is not None else None,
                )
            snapshot = self._snapshot(path, scope, content)
            changed = latest is None or latest[3] != snapshot.content_hash
            return ManualRead(
                snapshot=snapshot, diagnostics=(), disk_hash=disk_hash,
                revision=None if changed else latest[0],
                external_change=changed and (latest is not None or path.exists()),
            )

    def restore(
        self,
        scope: MemoryScope,
        revision: int,
        *,
        expected_hash: str,
        actor: str,
        reason: str,
        source_ref: str | None = None,
    ) -> MarkdownBookSnapshot:
        path = self._path(scope)
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT content FROM memory_document_versions WHERE scope_key=? AND revision=?",
                (scope.storage_key, revision),
            ).fetchone()
        if row is None:
            raise MemoryManualError(f"unknown MEMORY.md revision {revision}")
        return self._replace(
            scope, row[0], expected_hash=expected_hash, actor=actor,
            reason=f"restore:{revision}: {reason}", source_ref=source_ref,
            allow_invalid=True,
        )
    
    ## 更新对应的markdown逻辑
    def replace(
        self,
        scope: MemoryScope,
        content: str,
        *,
        expected_hash: str,
        actor: str,
        reason: str,
        source_ref: str | None = None,
    ) -> MarkdownBookSnapshot:
        return self._replace(
            scope, content, expected_hash=expected_hash, actor=actor,
            reason=reason, source_ref=source_ref, allow_invalid=False,
        )

    def _replace(
        self,
        scope: MemoryScope,
        content: str,
        *,
        expected_hash: str,
        actor: str,
        reason: str,
        source_ref: str | None,
        allow_invalid: bool,
    ) -> MarkdownBookSnapshot:
        path = self._path(scope)
        self._validate(content)
        if not expected_hash or not actor.strip() or not reason.strip():
            raise MemoryManualContentError("expected_hash、actor 和 reason 均不能为空")
        if actor == "model" and not source_ref:
            raise MemoryManualContentError("模型修改 MEMORY.md 必须提供来源引用")
        with _lock_for(path):
            current = self.read(scope)
            if current.diagnostics and not allow_invalid:
                raise MemoryManualFileInvalidError(
                    "磁盘 MEMORY.md 未通过校验；请先修正文件或显式恢复版本"
                )
            if expected_hash != current.disk_hash:
                raise MarkdownRevisionConflictError(
                    f"expected={expected_hash} actual={current.disk_hash}"
                )
            snapshot = self._snapshot(path, scope, content)
            if snapshot.content_hash == current.disk_hash:
                return snapshot
            before = path.read_bytes() if path.exists() else b""
            replaced = False
            try:
                with self._db.write() as conn:
                    latest = _latest(conn, scope.storage_key)
                    _insert_version(
                        conn, scope.storage_key,
                        (latest[0] + 1) if latest is not None else 1,
                        snapshot, actor=actor, reason=reason, source_ref=source_ref,
                    )
                    ensure_scope_dir(self._root, scope)
                    _atomic_replace(path, content.encode("utf-8"), expected_hash)
                    replaced = True
            except BaseException:
                # 文件和 SQLite 不能共同提交；数据库提交失败时恢复旧字节。
                if replaced:
                    with suppress(Exception):
                        _atomic_replace(path, before, snapshot.content_hash)
                raise
            return snapshot
    
    ## 只有个人用户有权限获取自己的memroy.md文件
    def _path(self, scope: MemoryScope) -> Path:
        if scope.user_id is None:
            raise MemoryManualScopeError("只有个人 scope 可以编辑 MEMORY.md")
        directory = self._root / scope.storage_key
        path = directory / "MEMORY.md"
        if directory.is_symlink() or path.is_symlink():
            raise MemoryManualScopeError("MEMORY.md 目录或文件不能是符号链接")
        if not path.resolve().is_relative_to(self._root):
            raise MemoryManualScopeError("MEMORY.md 路径越界")
        return path

    ## 校验markdown的逻辑并返回错误信息
    def _validate(self, content: str) -> None:
        if not isinstance(content, str):
            raise MemoryManualContentError("MEMORY.md 必须是文本")
        try:
            content.encode("utf-8")
        except UnicodeError as exc:
            raise MemoryManualContentError("MEMORY.md 包含无效 Unicode") from exc
        if _INVALID_CONTROLS.search(content):
            raise MemoryManualContentError("MEMORY.md 包含控制字符")
        if _CREDENTIAL_VALUE.search(content) or _API_SECRET.search(content) or _IDENTITY_NUMBER.search(content):
            raise MemoryManualContentError("MEMORY.md 不能包含凭证或身份证号")
        if self._token_counter(content) > self._max_tokens:
            raise MemoryManualContentError(
                f"MEMORY.md 超过 {self._max_tokens} 个估算 tokens，请合并或精简"
            )

    def _snapshot(self, path: Path, scope: MemoryScope, content: str) -> MarkdownBookSnapshot:
        return MarkdownBookSnapshot(
            path=str(path), layer=MemoryLayer.USER, type="long_term_fact",
            scope=scope, content=content, content_hash=content_sha256(content),
            token_count=max(0, int(self._token_counter(content))),
        )


def _bytes_hash(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        return hashlib.sha256(raw).hexdigest()
    return content_sha256(text)


def _atomic_replace(path: Path, content: bytes, expected_hash: str) -> None:
    raw = path.read_bytes() if path.exists() else b""
    actual_hash = _bytes_hash(raw)
    if actual_hash != expected_hash:
        raise MarkdownRevisionConflictError(f"expected={expected_hash} actual={actual_hash}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _latest(conn, scope_key: str) -> tuple[int, str, str, str] | None:
    return conn.execute(
        "SELECT revision,actor,content,content_hash FROM memory_document_versions "
        "WHERE scope_key=? ORDER BY revision DESC LIMIT 1",
        (scope_key,),
    ).fetchone()


def _insert_version(
    conn, scope_key: str, revision: int, snapshot: MarkdownBookSnapshot,
    *, actor: str, reason: str, source_ref: str | None,
) -> None:
    from ...storage.sqlite.codec import milliseconds

    conn.execute(
        "INSERT INTO memory_document_versions "
        "(scope_key,revision,content,content_hash,actor,reason,source_ref,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (scope_key, revision, snapshot.content, snapshot.content_hash,
         actor, reason, source_ref, milliseconds()),
    )


