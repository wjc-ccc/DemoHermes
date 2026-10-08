from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent.core.memory.markdown_book import MarkdownBook
from agent.core.memory.memory_item import UserMemoryItem, content_sha256
from agent.core.memory.types.card_types import UserCardStatus
from agent.core.memory.memory_layer import MemoryLayer
from agent.core.memory.memory_retrieval import MemoryRetriever
from agent.core.memory.memory_scope import MemoryScope
from agent.core.memory.types.memory_source_types import EvidenceRef
from agent.storage.sqlite import RuntimeStorage
from agent.tools.memory_tools import MemoryReadTool, MemorySearchTool


def _card(scope, identity, content, *, kind="reusable_experience", status=UserCardStatus.ACTIVE,
          expires=None, evidence=()):
    return UserMemoryItem(
        item_id=identity, kind=kind, source_type="user_authored", source_ref="message-1",
        scope=scope, content=content, content_hash=content_sha256(content), summary=content,
        slot_key=None if kind == "reusable_experience" else f"{kind}:assistant:{identity}",
        source_authority="user_explicit", status=status,
        valid_until=expires, keywords=("pytest", "Windows", "虚拟环境") if kind == "reusable_experience"
        else ("中文",), applicability="在 Windows 项目中运行 pytest" if kind == "reusable_experience" else None,
        action="先激活虚拟环境", outcome="本次测试成功", limitations="只验证过当前项目",
        outcome_status="success" if kind == "reusable_experience" else None,
        evidence=evidence,
    )


def test_recall_only_relevant_active_experience_with_budget_and_scope(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    user = MemoryScope(tenant_id="cli", user_id="u1")
    other = MemoryScope(tenant_id="cli", user_id="u2")
    storage.memory.put(_card(user, "experience", "Windows pytest 虚拟环境经验"))
    storage.memory.put(_card(user, "preference", "用户喜欢中文", kind="stable_preference"))
    storage.memory.put(_card(user, "quarantined", "pytest 隔离经验", status=UserCardStatus.QUARANTINED))
    storage.memory.put(_card(user, "expired", "pytest 过期经验",
                             expires=datetime.now(timezone.utc) - timedelta(days=1)))
    storage.memory.put(_card(other, "foreign", "pytest 别人经验"))
    retriever = MemoryRetriever(storage.db)

    hits = retriever.recall(user, "Windows 下如何运行 pytest？")
    assert [hit.reference for hit in hits] == ["card:experience"]
    assert "适用条件" in hits[0].content and "证据" not in hits[0].content
    assert retriever.recall(user, "Windows项目的pytest怎么运行？")[0].reference == "card:experience"
    assert retriever.recall(user, "我今天想写周报") == ()
    assert retriever.recall(user, "Windows 下如何运行 pytest？", token_budget=1) == ()
    assert retriever.recall(MemoryScope(tenant_id="cli:chat:group"), "pytest") == ()


def test_search_and_read_are_scope_bound_and_page_raw_sources(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    user = MemoryScope(tenant_id="cli", user_id="u1")
    other = MemoryScope(tenant_id="cli", user_id="u2")
    storage.memory.put(_card(user, "experience", "pytest 已验证经验",
                             evidence=(EvidenceRef(source_type="message", source_id="message-1"),)))
    session = storage.sessions.get_or_create("slot", scope=user, channel="cli", author_id="u1")
    with storage.db.write() as conn:
        conn.execute(
            "INSERT INTO messages (message_id,session_id,turn_id,seq_id,role,content_text,"
            "source_type,created_at,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("message-1", session.session_id, None, 0, "user", "pytest 原始问题和详细上下文",
             "user_authored", 1000, "{}"),
        )
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id,session_id,turn_id,trajectory_id,tool_name,status,"
            "created_at,payload_json) VALUES (?,?,?,?,?,?,?,?)",
            ("tool-1", session.session_id, None, "turn-1", "bash", "success", 2000,
             json.dumps({"result_text": "pytest 收集到 42 个测试"}, ensure_ascii=False)),
        )
    memory_root = tmp_path / "memory"
    path = memory_root / user.storage_key / "MEMORY.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 手册\npytest 项目约定", encoding="utf-8")
    snapshot = MarkdownBook(path, layer=MemoryLayer.USER, type="long_term_fact", scope=user).read()
    document_id = storage.documents.register(snapshot)
    outside = tmp_path / "outside.md"
    outside.write_text("pytest 私有文件", encoding="utf-8")
    outside_id = storage.documents.register(
        MarkdownBook(outside, layer=MemoryLayer.USER, type="long_term_fact", scope=user).read())
    retriever = MemoryRetriever(storage.db, document_root=memory_root)

    card_hits = retriever.search(user, "pytest", source="card")
    assert [h.reference for h in card_hits] == ["card:experience"]
    assert card_hits[0].evidence_refs == ("message:message-1",)
    assert [h.reference for h in retriever.search(user, "pytest", source="history")] == ["message:message-1"]
    raw_refs = {h.reference for h in retriever.search(user, "pytest", source="raw")}
    assert raw_refs == {"tool:tool-1", f"document:{document_id}"}
    assert retriever.read(user, f"document:{outside_id}") is None
    page = retriever.read(user, "message:message-1", limit=6)
    assert page.content == "pytest" and page.next_offset == 6
    assert retriever.read(user, "message:message-1", offset=page.next_offset).content.startswith(" 原始问题")
    assert "42 个测试" in retriever.read(user, "tool:tool-1").content
    assert "项目约定" in retriever.read(user, f"document:{document_id}").content
    for reference in ("card:experience", "message:message-1", "tool:tool-1", f"document:{document_id}"):
        assert retriever.read(other, reference) is None
    assert retriever.search(other, "pytest") == ()

    assert (card_hits[0].item_id, card_hits[0].revision) == ("experience", 1)
    with storage.db.write() as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id,session_id,turn_id,trajectory_id,tool_name,status,"
            "created_at,payload_json) VALUES (?,?,?,?,?,?,?,?)",
            ("tool-memory", session.session_id, None, "turn-2", "memory_search", "success", 3000,
             json.dumps({"result_text": "pytest 旧检索结果"}, ensure_ascii=False)),
        )
    assert "tool:tool-memory" not in {h.reference for h in retriever.search(user, "pytest", source="raw")}

    search_tool, read_tool = MemorySearchTool(retriever), MemoryReadTool(retriever)
    assert search_tool.execute({"query": "pytest"}).ok is False
    assert search_tool.execute({"_scope": user, "query": "pytest", "mode": "semantic"}).ok is False
    result = search_tool.execute({"_scope": user, "query": "pytest", "source": "history"})
    assert result.ok and result.data["hits"][0]["reference"] == "message:message-1"
    assert read_tool.execute({"_scope": other, "reference": "message:message-1"}).ok is False
