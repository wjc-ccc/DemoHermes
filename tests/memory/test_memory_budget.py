"""Slice 5: 预算、备份与谨慎缩减——Hot 字符硬上限（§4.2 / §4.4）。"""
import pytest

from Agent.memory import (
    BuiltinMemoryRequest,
    MemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="user_request")

PROFILE_MAX_CHARS = 1375  # §4.2 Hermes 默认：USER.md / GROUP.md 字符硬上限


def test_profile_at_exact_limit_is_accepted(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory", id_factory=lambda: "e_01JABC")
    # §3.5 文档格式字面量："# USER\n\n<!-- memory-entry:e_01JABC -->\n{C}\n<!-- /memory-entry:e_01JABC -->\n"
    # 长度 = 6+2+30+1+1+31+1 + len(C) = 72 + len(C)；精确上限 → len(C) = 1303
    content = "好" * 1303
    result = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content=content, provenance=_PROVENANCE,
        )
    )

    assert result.ok, result.message
    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert len(view.rendered) == PROFILE_MAX_CHARS


class _FixedCompactor:
    """FakeCompactor：返回固定字面量（§10.1-8 允许的外部 seam）。"""

    def __init__(self, output: str):
        self._output = output
        self.calls: list[tuple[int, int]] = []

    def compact(self, candidate: str, max_chars: int) -> str:
        self.calls.append((len(candidate), max_chars))
        return self._output


def test_oversized_candidate_is_backed_up_then_compacted(tmp_path):
    root = tmp_path / "memory"
    compacted_doc = "# USER\n\n<!-- memory-entry:e_01JABC -->\n精简后的偏好\n<!-- /memory-entry:e_01JABC -->\n"
    compactor = _FixedCompactor(compacted_doc)
    manager = MemoryManager(
        root=root, id_factory=_sequential_ids(), compactor=compactor
    )
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="长" * 1200, provenance=_PROVENANCE,
        )
    )
    assert first.ok  # 第一条文档 1284 字符，在 1375 内

    oversized = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="顶过上限" * 30, provenance=_PROVENANCE,  # 候选 1482 字符，超限
        )
    )

    assert oversized.ok
    assert oversized.compacted
    assert oversized.backup_id is not None
    # compactor 见到的是完整超限候选与 profile 上限
    assert compactor.calls and compactor.calls[0][0] > PROFILE_MAX_CHARS
    assert compactor.calls[0][1] == PROFILE_MAX_CHARS
    # operator seam：备份含完整超限候选（两条 entry 都在）
    backups = list(root.rglob("backups/profile/*.md"))
    assert len(backups) == 1
    backup_text = backups[0].read_text(encoding="utf-8")
    assert "长" * 1200 in backup_text and "顶过上限" * 30 in backup_text
    # 正式文件是缩减后的合法文档
    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.rendered == compacted_doc
    assert len(view.rendered) <= PROFILE_MAX_CHARS


def _sequential_ids():
    counter = iter(range(1, 100))
    return lambda: f"e_{next(counter):012d}"


class _ExplodingCompactor:
    def compact(self, candidate: str, max_chars: int) -> str:
        raise RuntimeError("compactor 内部崩溃")


def test_compaction_failure_keeps_previous_document_unchanged(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(
        root=root, id_factory=_sequential_ids(), compactor=_ExplodingCompactor()
    )
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="长" * 1200, provenance=_PROVENANCE,
        )
    )
    assert first.ok

    failed = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="顶过上限" * 30, provenance=_PROVENANCE,
        )
    )

    assert not failed.ok
    assert failed.error_code == "budget_exceeded"
    assert failed.backup_id is not None  # 完整候选仍已备份，可人工恢复
    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.revision == first.revision
    assert "顶过上限" not in view.rendered
    backups = list(root.rglob("backups/profile/*.md"))
    assert len(backups) == 1
    assert "顶过上限" * 30 in backups[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_output",
    [
        "长" * 2000,  # 仍超限
        "# USER\n\n<!-- memory-entry:e_x -->\n只有开围栏没有闭围栏\n",  # 格式损坏
        "没有标题的纯文本",  # 丢失文档标题
    ],
    ids=["still_oversized", "broken_entry_fence", "missing_title"],
)
def test_invalid_compactor_output_is_rejected(tmp_path, bad_output):
    manager = MemoryManager(
        root=tmp_path / "memory",
        id_factory=_sequential_ids(),
        compactor=_FixedCompactor(bad_output),
    )
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="长" * 1200, provenance=_PROVENANCE,
        )
    )
    assert first.ok

    rejected = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="顶过上限" * 30, provenance=_PROVENANCE,
        )
    )

    assert not rejected.ok
    assert rejected.error_code == "budget_exceeded"
    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.revision == first.revision


def test_backup_write_failure_keeps_previous_document_unchanged(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(
        root=root, id_factory=_sequential_ids(), compactor=_FixedCompactor("# USER\n")
    )
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="长" * 1200, provenance=_PROVENANCE,
        )
    )
    assert first.ok
    # operator seam：把 scope 目录下的 backups 占位成一个普通文件，备份写入必然失败
    scope_dir = next(p for p in root.iterdir() if p.is_dir())
    (scope_dir / "backups").write_text("blocked", encoding="utf-8")

    failed = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="顶过上限" * 30, provenance=_PROVENANCE,
        )
    )

    assert not failed.ok
    assert failed.error_code == "backup_failed"
    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.revision == first.revision
    assert "顶过上限" not in view.rendered


def test_externally_oversized_file_builds_bounded_context_with_diagnostic(tmp_path):
    """§4.4 末段：用户手工把正式文件改到超限后，build_context 不改磁盘，
    只在 entry 边界生成有界降级视图并附 oversized_document diagnostic。"""
    root = tmp_path / "memory"
    manager = MemoryManager(root=root, id_factory=_sequential_ids())
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="profile", action="add",
            content="甲" * 1000, provenance=_PROVENANCE,
        )
    )
    assert first.ok  # 文档 1084 字符，在 1375 内

    # operator seam：模拟用户手工编辑，把第二条 entry 直接写进 USER.md（超限量）
    scope_dir = next(p for p in root.iterdir() if p.is_dir())
    profile_path = scope_dir / "USER.md"
    oversized_text = (
        profile_path.read_text(encoding="utf-8").rstrip("\n")
        + "\n\n<!-- memory-entry:e_manual -->\n"
        + "乙" * 800
        + "\n<!-- /memory-entry:e_manual -->\n"
    )  # 文档 1950 字符，超 1375
    profile_path.write_text(oversized_text, encoding="utf-8", newline="\n")
    before_build = profile_path.read_text(encoding="utf-8")

    snapshot = manager.build_context(scope=_SCOPE, query="你好", session_id="s1")

    # build_context 不修改磁盘
    assert profile_path.read_text(encoding="utf-8") == before_build
    # 附带 oversized_document diagnostic
    assert any(d.code == "oversized_document" for d in snapshot.diagnostics)
    # prompt 中 profile 段有界，且裁在 entry 边界：
    # 第一条完整保留，第二条整条不出现，围栏不成对破损
    assert "甲" * 1000 in snapshot.prompt
    assert "乙" * 800 not in snapshot.prompt
    assert snapshot.prompt.count("<!-- memory-entry:") == snapshot.prompt.count(
        "<!-- /memory-entry:"
    )
    profile_block = snapshot.prompt.split("<profile-memory>\n", 1)[1].split(
        "\n</profile-memory>", 1
    )[0]
    assert len(profile_block) <= PROFILE_MAX_CHARS
