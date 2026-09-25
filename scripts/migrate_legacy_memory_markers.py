"""把旧 marker 记忆迁入 SQLite，并从 MEMORY.md 移除协议围栏。

默认只输出报告；只有显式传入 ``--apply --db PATH`` 才会写入。每份文档在
首次写入前保存 ``.legacy-marker.bak`` 备份。该解析器仅用于一次性迁移，运行时
MemoryContextLoader 不导入也不调用本模块。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.storage.sqlite import RuntimeStorage
from agent.core.memory import (
    MarkdownBook,
    UserMemoryKind,
    MemoryLayer,
    MemoryScope,
    UserMemoryItem,
    UserMemoryStatus,
    content_sha256,
)
from agent.storage.sqlite import SqliteMemoryStore

_ENTRY_RE = re.compile(
    r"<!-- memory-entry:(?P<id>\S+) -->\r?\n"
    r"(?P<content>.*?)\r?\n"
    r"<!-- /memory-entry:(?P=id) -->",
    re.DOTALL,
)
_OPEN = "<!-- memory-entry:"
_CLOSE = "<!-- /memory-entry:"


@dataclass(frozen=True)
class LegacyMarkerReport:
    path: str
    entry_count: int
    unmatched_marker_count: int
    imported_item_ids: tuple[str, ...] = ()
    rewritten: bool = False
    backup_path: str | None = None


def _legacy_scope(path: Path) -> MemoryScope:
    scope_path = path.with_name("scope.json")
    if not scope_path.exists():
        return MemoryScope()
    raw = json.loads(scope_path.read_text(encoding="utf-8"))
    known = {
        key: raw.get(key)
        for key in ("organization_id", "tenant_id", "user_id", "project_id")
        if raw.get(key) is not None
    }
    if known:
        return MemoryScope(**known)
    legacy_key = str(raw.get("key") or "")
    parts = legacy_key.split(":")
    if len(parts) >= 3 and parts[1] == "user":
        return MemoryScope(tenant_id=parts[0], user_id=":".join(parts[2:]))
    return MemoryScope(tenant_id=legacy_key or None)


def _item(scope: MemoryScope, path: Path, entry_id: str, content: str) -> UserMemoryItem:
    digest = hashlib.sha256(
        f"{path.resolve()}\0{entry_id}\0{content}".encode("utf-8")
    ).hexdigest()
    procedural = content.lstrip().startswith("[playbook]")
    return UserMemoryItem(
        item_id=f"legacy-{digest[:24]}",
        type="reusable_experience" if procedural else "long_term_fact",
        memory_kind=UserMemoryKind.PROCEDURAL if procedural else UserMemoryKind.SEMANTIC,
        normalized_key=f"legacy.{digest[:32]}",
        status=UserMemoryStatus.PROPOSED,
        confidence=0.5,
        source_authority="legacy_import",
        source_type="import",
        source_ref=f"{path}:{entry_id}",
        scope=scope,
        content=content,
        summary=content[:240],
        token_count=len(content) if not content.isascii() else len(content.split()),
        content_hash=content_sha256(content),
        metadata={"legacy_entry_id": entry_id, "legacy_path": str(path)},
    )


def migrate_document(
    path: str | Path,
    *,
    db_path: str | Path | None = None,
    apply: bool = False,
) -> LegacyMarkerReport:
    document = Path(path)
    text = document.read_text(encoding="utf-8")
    matches = tuple(_ENTRY_RE.finditer(text))
    unmatched = text.count(_OPEN) + text.count(_CLOSE) - len(matches) * 2
    if not apply or not matches:
        return LegacyMarkerReport(
            path=str(document),
            entry_count=len(matches),
            unmatched_marker_count=max(0, unmatched),
        )
    if db_path is None:
        raise ValueError("--apply 必须同时提供 --db")
    if unmatched:
        raise ValueError(f"{document} 存在未配对 marker，拒绝自动改写")

    backup = document.with_suffix(document.suffix + ".legacy-marker.bak")
    if backup.exists():
        if backup.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"备份已存在且内容不同，拒绝覆盖: {backup}")
    else:
        shutil.copy2(document, backup)

    scope = _legacy_scope(document)
    store = RuntimeStorage.open(db_path).memory
    imported = tuple(
        store.put(_item(scope, document, match.group("id"), match.group("content").strip())).item_id
        for match in matches
    )
    cleaned = _ENTRY_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip() + "\n"
    MarkdownBook(
        document,
        layer=MemoryLayer.USER,
        type="long_term_fact",
        scope=scope,
        writable=True,
    ).replace(cleaned, expected_hash=content_sha256(text))
    return LegacyMarkerReport(
        path=str(document),
        entry_count=len(matches),
        unmatched_marker_count=0,
        imported_item_ids=imported,
        rewritten=True,
        backup_path=str(backup),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="只扫描该目录下的 MEMORY.md")
    parser.add_argument("--db", type=Path, help="应用迁移时写入的 SQLite 数据库")
    parser.add_argument("--apply", action="store_true", help="备份、导入并移除 marker")
    args = parser.parse_args()
    root = args.root.resolve()
    reports = [
        migrate_document(path, db_path=args.db, apply=args.apply)
        for path in sorted(root.rglob("MEMORY.md"))
        if path.resolve().is_relative_to(root)
    ]
    print(json.dumps([asdict(report) for report in reports], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
