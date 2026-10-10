"""Markdown 文档的统一读取、个人手册编辑与版本恢复入口。"""
from __future__ import annotations
from agent.types.memory.document import MarkdownRevisionConflictError, MemoryManualContentError, MemoryManualError, MemoryManualFileInvalidError, MemoryManualScopeError
from agent.core.memory.document_text import estimate_markdown_tokens

import hashlib
import os
import re
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from agent.types.memory.document import DocumentRead
from agent.types.memory.items import content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.snapshot import MarkdownBookSnapshot, MemoryDiagnostic
from agent.types.memory.layers import MemoryLayer
from agent.core.memory.memory_scope import ensure_scope_dir

if TYPE_CHECKING:
    from agent.storage.sqlite.database import Database
















### 统一的内存锁，用于防止多个线程同时访问同一个文件
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:password|passwd|api[_ -]?key|secret|access[_ -]?token|token|密码|密钥|访问令牌)\s*[:=：]\s*\S+"
)
_API_SECRET = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")
_IDENTITY_NUMBER = re.compile(r"\b\d{17}[\dXx]\b")
_INVALID_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())





def _decode(raw: bytes) -> str:
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _bytes_hash(raw: bytes) -> str:
    try:
        return content_sha256(_decode(raw))
    except UnicodeError:
        return hashlib.sha256(raw).hexdigest()


def _atomic_replace(path: Path, content: bytes, expected_hash: str) -> None:
    raw = path.read_bytes() if path.exists() else b""
    actual = _bytes_hash(raw)
    if actual != expected_hash:
        raise MarkdownRevisionConflictError(f"expected={expected_hash} actual={actual}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


class MarkdownDocuments:
    """读取统一文档；写入始终定位当前个人 scope 的 MEMORY.md。

    read(persist=False) 使用同一校验和有效版本回退，不登记人工修改。
    db 可省略用于仅文件读取；生产装配共享同一实例及数据库。
    """

    def __init__(self, root: str | Path, db: Database | None = None, *,
                 policy_paths: Iterable[str | Path] = (), project_paths: Iterable[str | Path] = (),
                 max_tokens: int = 1200,
                 token_counter: Callable[[str], int] = estimate_markdown_tokens) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self._root = Path(root).resolve()
        self._db = db
        self.policy_paths = tuple(Path(p).resolve() for p in policy_paths)
        self.project_paths = tuple(Path(p).resolve() for p in project_paths)
        self._max_tokens = max_tokens
        self._token_counter = token_counter

    def _user_path(self, scope: MemoryScope) -> Path:
        if not isinstance(scope, MemoryScope) or scope.scope_kind != "personal":
            raise MemoryManualScopeError("只有个人 scope 可以访问 MEMORY.md")
        directory = self._root / scope.storage_key
        path = directory / "MEMORY.md"
        if directory.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(self._root):
            raise MemoryManualScopeError("MEMORY.md 路径越界或为符号链接")
        return path

    def _target(self, scope: MemoryScope, layer: MemoryLayer, path: str | Path | None) -> tuple[Path, MemoryScope]:
        if not isinstance(scope, MemoryScope):
            raise MemoryManualScopeError("缺少运行时 memory scope")
        if layer == MemoryLayer.USER:
            target = self._user_path(scope) if path is None else Path(path)
            directory = self._root / scope.storage_key
            if scope.scope_kind != "personal" or directory.is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(directory.resolve()):
                raise MemoryManualScopeError("文档不属于当前用户")
            return target.resolve(), scope
        allowed = self.policy_paths if layer == MemoryLayer.POLICY else self.project_paths if layer == MemoryLayer.PROJECT else ()
        if path is None or Path(path).resolve() not in allowed:
            raise MemoryManualScopeError("文档不在配置的只读路径中")
        owner = MemoryScope(organization_id=scope.organization_id) if layer == MemoryLayer.POLICY else MemoryScope(
            organization_id=scope.organization_id, tenant_id=scope.tenant_id, project_id=scope.project_id)
        return Path(path).resolve(), owner

    def read(self, scope: MemoryScope, layer: MemoryLayer = MemoryLayer.USER,
             path: str | Path | None = None, *, persist: bool = True) -> DocumentRead:
        layer = MemoryLayer(layer)
        target, owner = self._target(scope, layer, path)
        is_manual = layer == MemoryLayer.USER and target == self._user_path(scope).resolve()
        with _lock_for(target):
            raw = target.read_bytes() if target.exists() else b""
            disk_hash = _bytes_hash(raw)
            latest = None
            if is_manual and self._db is not None:
                with self._db.read() as conn:
                    latest = _latest(conn, scope.storage_key)
            try:
                content = _decode(raw)
                if is_manual:
                    self._validate(content)
            except (UnicodeError, MemoryManualContentError) as exc:
                if not is_manual:
                    raise
                return DocumentRead(
                    snapshot=self._snapshot(target, owner, layer, latest[2] if latest else ""),
                    diagnostics=(MemoryDiagnostic(code="memory_document_invalid", message=f"MEMORY.md: {exc}"),),
                    disk_hash=disk_hash, revision=latest[0] if latest else None)
            snapshot = self._snapshot(target, owner, layer, content)
            revision = None
            if is_manual and self._db is not None:
                changed = latest is None or latest[3] != snapshot.content_hash
                if persist and changed:
                    with self._db.write() as conn:
                        latest = _latest(conn, scope.storage_key)
                        if latest is None or latest[3] != snapshot.content_hash:
                            revision = latest[0] + 1 if latest else 1
                            _insert_version(conn, scope.storage_key, revision, snapshot,
                                            actor="human" if target.exists() else "system",
                                            reason="external_edit" if target.exists() else "bootstrap", source_ref=None)
                        else:
                            revision = latest[0]
                elif not changed:
                    revision = latest[0]
            return DocumentRead(snapshot=snapshot, diagnostics=(), disk_hash=disk_hash, revision=revision)

    def read_revision(self, scope: MemoryScope, revision: int) -> str | None:
        self._user_path(scope)
        if self._db is None or revision < 1:
            return None
        with self._db.read() as conn:
            row = conn.execute("SELECT content FROM memory_document_versions WHERE scope_key=? AND revision=?",
                               (scope.storage_key, revision)).fetchone()
        return row[0] if row else None

    def restore_user(self, scope: MemoryScope, revision: int, *, expected_hash: str,
                     actor: str, reason: str, source_ref: str | None = None) -> MarkdownBookSnapshot:
        content = self.read_revision(scope, revision)
        if content is None:
            raise MemoryManualError(f"unknown MEMORY.md revision {revision}")
        return self._replace_user(scope, content, expected_hash=expected_hash, actor=actor,
                                  reason=f"restore:{revision}: {reason}", source_ref=source_ref, allow_invalid=True)

    def replace_user(self, scope: MemoryScope, content: str, *, expected_hash: str,
                     actor: str, reason: str, source_ref: str | None = None) -> MarkdownBookSnapshot:
        return self._replace_user(scope, content, expected_hash=expected_hash, actor=actor,
                                  reason=reason, source_ref=source_ref, allow_invalid=False)

    def _replace_user(self, scope: MemoryScope, content: str, *, expected_hash: str,
                      actor: str, reason: str, source_ref: str | None, allow_invalid: bool) -> MarkdownBookSnapshot:
        path = self._user_path(scope)
        content = content.replace("\r\n", "\n").replace("\r", "\n") if isinstance(content, str) else content
        self._validate(content)
        if not expected_hash or not actor.strip() or not reason.strip():
            raise MemoryManualContentError("expected_hash、actor 和 reason 均不能为空")
        if actor == "model" and not source_ref:
            raise MemoryManualContentError("模型修改 MEMORY.md 必须提供来源引用")
        with _lock_for(path):
            current = self.read(scope)
            if current.diagnostics and not allow_invalid:
                raise MemoryManualFileInvalidError("磁盘 MEMORY.md 未通过校验；请先修正文件或显式恢复版本")
            if expected_hash != current.disk_hash:
                raise MarkdownRevisionConflictError(f"expected={expected_hash} actual={current.disk_hash}")
            snapshot = self._snapshot(path, scope, MemoryLayer.USER, content)
            if snapshot.content_hash == current.disk_hash:
                return snapshot
            before = path.read_bytes() if path.exists() else b""
            replaced = False
            try:
                if self._db is None:
                    _atomic_replace(path, content.encode("utf-8"), expected_hash)
                else:
                    with self._db.write() as conn:
                        latest = _latest(conn, scope.storage_key)
                        _insert_version(conn, scope.storage_key, latest[0] + 1 if latest else 1, snapshot,
                                        actor=actor, reason=reason, source_ref=source_ref)
                        ensure_scope_dir(self._root, scope)
                        _atomic_replace(path, content.encode("utf-8"), expected_hash)
                        replaced = True
            except BaseException:
                if replaced:
                    with suppress(Exception):
                        _atomic_replace(path, before, snapshot.content_hash)
                raise
            return snapshot

    ## 验证memory.md的内容是否合法
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
            raise MemoryManualContentError(f"MEMORY.md 超过 {self._max_tokens} 个估算 tokens，请合并或精简")
    ## 返回markdown的快照
    def _snapshot(self, path: Path, scope: MemoryScope, layer: MemoryLayer, content: str) -> MarkdownBookSnapshot:
        return MarkdownBookSnapshot(path=str(path), layer=layer,
            type={MemoryLayer.POLICY: "compliance_rule", MemoryLayer.PROJECT: "project_rule", MemoryLayer.USER: "long_term_fact"}[layer],
            scope=scope, content=content, content_hash=content_sha256(content),
            token_count=max(0, int(self._token_counter(content))))

## 返回最新可用版本
def _latest(conn, scope_key: str):
    return conn.execute("SELECT revision,actor,content,content_hash FROM memory_document_versions WHERE scope_key=? ORDER BY revision DESC LIMIT 1",
                        (scope_key,)).fetchone()


def _insert_version(conn, scope_key: str, revision: int, snapshot: MarkdownBookSnapshot, *, actor: str, reason: str, source_ref: str | None) -> None:
    from agent.storage.sqlite.codec import milliseconds
    conn.execute("INSERT INTO memory_document_versions (scope_key,revision,content,content_hash,actor,reason,source_ref,created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (scope_key, revision, snapshot.content, snapshot.content_hash, actor, reason, source_ref, milliseconds()))

