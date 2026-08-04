"""Phase 2.1：GrowAndRefineCompactor（8.04）——结构化缩减，禁止散文摘要。

公共 seam：
  - GrowAndRefineCompactor.compact(candidate, max_chars) -> 合法 Markdown
  - MemoryManager(compactor=...) 超限 apply 成功且关键 playbook 存活
"""
from __future__ import annotations

from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.memory.budget import MEMORY_MAX_CHARS
from Agent.memory.hot.store import render_entry

_SCOPE = MemoryScope(kind="personal", key="cli:user:c2")
_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="t_seed")


def _doc(title: str, entries: list[tuple[str, str]]) -> str:
    parts = [f"# {title}\n"]
    for eid, content in entries:
        parts.append("\n" + render_entry(eid, content) + "\n")
    return "".join(parts)


def test_compactor_dedupes_near_duplicate_entries():
    """近重复 entry 只保留一条。"""
    from Agent.evolution.compactor import GrowAndRefineCompactor

    candidate = _doc(
        "MEMORY",
        [
            ("e_a", "[playbook] 超时先重试"),
            ("e_b", "[playbook] 超时先重试"),
            ("e_c", "[playbook] 部署前跑测试"),
        ],
    )
    out = GrowAndRefineCompactor().compact(candidate, max_chars=MEMORY_MAX_CHARS)
    assert out.count("[playbook] 超时先重试") == 1
    assert "部署前跑测试" in out
    assert "<!-- memory-entry:e_a -->" in out or "<!-- memory-entry:e_b -->" in out


def test_compactor_drops_harmful_before_healthy_playbook():
    """超限时先丢 [playbook][harmful]，保留健康 playbook。"""
    from Agent.evolution.compactor import GrowAndRefineCompactor

    healthy = "[playbook] " + ("保" * 80)
    harmful = "[playbook][harmful] " + ("害" * 80)
    filler = "普通笔记 " + ("记" * 80)
    # 构造必然超限的小预算
    max_chars = 400
    candidate = _doc(
        "MEMORY",
        [
            ("e1", filler),
            ("e2", harmful),
            ("e3", healthy),
        ],
    )
    assert len(candidate) > max_chars

    out = GrowAndRefineCompactor().compact(candidate, max_chars=max_chars)

    assert len(out) <= max_chars
    assert "保" * 10 in out  # 健康 playbook 存活
    assert "[playbook][harmful]" not in out


def test_compactor_does_not_rewrite_into_short_summary():
    """禁止把多条策略压成一句摘要（anti brevity / collapse）。"""
    from Agent.evolution.compactor import GrowAndRefineCompactor

    tip_a = "[playbook] 工具超时时先重试一次再报告完整错误"
    tip_b = "[playbook] 部署前必须先跑 pytest 全绿"
    candidate = _doc("MEMORY", [("e1", tip_a), ("e2", tip_b)])
    out = GrowAndRefineCompactor().compact(candidate, max_chars=MEMORY_MAX_CHARS)

    # 在预算充足时两条原文都必须完整保留，不得变成「总结：注意超时与部署」
    assert tip_a in out
    assert tip_b in out


def test_manager_oversized_apply_succeeds_with_grow_and_refine(tmp_path):
    """注入 GrowAndRefineCompactor 后，超 MEMORY 上限的 apply 转为整理成功。"""
    from Agent.evolution.compactor import GrowAndRefineCompactor

    manager = MemoryManager(
        root=tmp_path / "memory",
        id_factory=_sequential_ids(),
        compactor=GrowAndRefineCompactor(),
    )
    # 先塞满接近上限的条目
    chunk = "[playbook] 策略" + ("X" * 180)
    while True:
        view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
        if len(view.rendered) + len(chunk) + 80 > MEMORY_MAX_CHARS:
            break
        r = manager.apply(
            BuiltinMemoryMutation(
                scope=_SCOPE, target="memory", action="add",
                content=chunk + f"-{len(view.entries)}",
                provenance=_PROVENANCE,
            )
        )
        assert r.ok, r.message

    # 再加一条会导致超限；有 Compactor 时应 compacted 成功
    vital = "[playbook] 关键策略：提交前必须跑回归测试套件"
    result = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="memory", action="add",
            content=vital,
            provenance=_PROVENANCE,
        )
    )
    assert result.ok, result.message
    assert result.compacted is True
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert len(view.rendered) <= MEMORY_MAX_CHARS
    assert "提交前必须跑回归测试套件" in view.rendered


def _sequential_ids():
    counter = iter(range(1, 1000))
    return lambda: f"e_{next(counter):012d}"
