"""Phase 5.2 / 5.3：环境快照 + 进化审计。

公共 seam：
  - EnvironmentSnapshotter.capture(...) → snapshot；store.get(id)
  - EvolutionAuditLog.append(...) / list_recent(n)
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from Agent.evolution.audit import EvolutionAuditEntry, EvolutionAuditLog
from Agent.evolution.env_snapshot import EnvironmentSnapshotter


def test_env_snapshot_captures_runtime_hashes(tmp_path: Path):
    """快照含 cwd / model / prompt_hash / tool_schema_hash / skill_versions。"""
    prompt = tmp_path / "systemPrompt.md"
    prompt.write_text("hello agent", encoding="utf-8")
    store_path = tmp_path / "env.db"
    snapper = EnvironmentSnapshotter(store_path)
    snap = snapper.capture(
        session_id="s1",
        turn_id="t1",
        cwd=str(tmp_path),
        model_name="fake-model",
        system_prompt_path=prompt,
        tool_schemas=[{"name": "calculator", "parameters": {"type": "object"}}],
        skill_versions={"weekly_report": "active"},
        git={"branch": "feat/x", "head": "abc"},
    )
    assert snap.session_id == "s1"
    assert snap.turn_id == "t1"
    assert snap.cwd == str(tmp_path)
    assert snap.runtime["model_name"] == "fake-model"
    assert snap.runtime["prompt_hash"] == hashlib.sha256(b"hello agent").hexdigest()
    assert "tool_schema_hash" in snap.runtime
    assert snap.runtime["skill_versions"]["weekly_report"] == "active"
    assert snap.git["branch"] == "feat/x"
    loaded = snapper.get(snap.snapshot_id)
    assert loaded is not None
    assert loaded.snapshot_id == snap.snapshot_id


def test_audit_log_records_evolution_deltas(tmp_path: Path):
    """审计表记录谁在何时对哪条 entry 做了何种 delta。"""
    log = EvolutionAuditLog(tmp_path / "audit.db")
    entry = EvolutionAuditEntry(
        actor="PlaybookCurator",
        op="add",
        target="memory",
        content_preview="[playbook] 超时先重试",
        turn_id="t9",
        reason="ok",
        admitted=True,
        scope_key="cli:user:u1",
    )
    aid = log.append(entry)
    recent = log.list_recent(10)
    assert len(recent) == 1
    assert recent[0].audit_id == aid
    assert recent[0].actor == "PlaybookCurator"
    assert recent[0].admitted is True
    assert "超时" in recent[0].content_preview
