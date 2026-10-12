"""Memory 行为评测：预先声明事实预期，核对提取、分类和额外卡片。

职责：用可复查的独立样例评估生产链路，保存原文、卡片、作业和检索证据。
边界：关键词断言只是语义检查的近似；单次通过不代表所有自然语言都正确。
关联：通过 runtime.build_runtime 启动 HTTP 后端，用公开 Memory 工具核对读取。
"""
from __future__ import annotations
from agent.types.tools import ToolInvocationContext
from agent.types.runtime import RuntimeConfig

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.types.memory.scope import MemoryScope
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from ..gateway.http_api import FrontierHttpServer
from ..storage.sqlite.database import Database
from ..storage.sqlite.memory_store import SqliteMemoryStore
from ..tools.memory_tools import MemoryReadTool, MemorySearchTool
from ..model.registry import ModelRegistry


class _RecordedModel:
    """只记录外部模型边界，不修改提示词、模型回复或后台提取结果。"""

    def __init__(self, model, path: Path):
        self.model, self.path = model, path
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.model, name)

    def chat(self, messages, tools=None, **kwargs):
        record = dict(started_at=time.time(), messages=messages, tools=tools)
        try:
            response = self.model.chat(messages, tools=tools, **kwargs)
            record["text"] = response.text
            record["tool_calls"] = [call.model_dump(mode="json") for call in response.tool_calls]
            return response
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["finished_at"] = time.time()
            with self.lock, self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def score_cards(expected: list[dict], cards: list[dict], *, strict_count: bool = True,
                forbidden_memories: list[dict] = ()) -> dict:
    """每个预期必须对应独立卡片；不能用同一张大杂烩卡匹配多个事实。"""
    candidates = []
    for spec in expected:
        matches = []
        for index, card in enumerate(cards):
            # 不用证据原文或关键词充当卡片主张，防止空泛卡引用整段原文而过关。
            claim = "\n".join(str(card.get(key) or "") for key in (
                "content", "applicability", "action", "outcome", "limitations",
            )).casefold()
            if (card["kind"] == spec["kind"]
                and all(any(term.casefold() in claim for term in group)
                        for group in spec["anchors"])
                and (not spec.get("outcome_status")
                     or card.get("outcome_status") == spec["outcome_status"])):
                matches.append(index)
        candidates.append(matches)

    assigned: dict[int, int] = {}

    def assign(expected_index: int, visited: set[int]) -> bool:
        for card_index in candidates[expected_index]:
            if card_index in visited:
                continue
            visited.add(card_index)
            if card_index not in assigned or assign(assigned[card_index], visited):
                assigned[card_index] = expected_index
                return True
        return False

    for index in range(len(expected)):
        assign(index, set())
    matched = {value: cards[key]["item_id"] for key, value in assigned.items()}
    issues = [dict(code="missing_expected_card", expected_index=index, expected=spec)
              for index, spec in enumerate(expected) if index not in matched]
    if strict_count:
        issues.extend(dict(code="unexpected_card", card_id=card["item_id"], kind=card["kind"])
                      for index, card in enumerate(cards) if index not in assigned)
    for rule in forbidden_memories:
        for card in cards:
            claim = (card.get("content") or "").casefold()
            if ((rule.get("kind") is None or card["kind"] == rule["kind"])
                and all(any(term.casefold() in claim for term in group) for group in rule["anchors"])):
                issues.append(dict(code="forbidden_memory", card_id=card["item_id"], description=rule["description"]))
    return dict(passed=not issues, matched=matched, issues=issues,
                expected_count=len(expected), actual_count=len(cards), matched_count=len(matched))


def _http(port: int, path: str, body: dict | None = None) -> dict:
    connection = HTTPConnection("127.0.0.1", port, timeout=180)
    try:
        connection.request("POST" if body is not None else "GET", path,
                           body=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        if response.status != 200 or payload.get("error"):
            raise RuntimeError(f"{path}: HTTP {response.status}: {payload.get('error')}")
        return payload
    finally:
        connection.close()


def _database_snapshot(db: Database, scope: MemoryScope) -> dict:
    """用户要求核对数据库，因此额外保留只读审计；功能断言仍通过公开工具完成。"""
    with db.read() as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        jobs = [dict(row) for row in connection.execute(
            "SELECT j.id,j.status,j.handler_version,j.attempt_count,j.input_json,j.result_json,j.last_error "
            "FROM jobs j JOIN sessions s ON s.session_id=j.session_id "
            "WHERE s.scope_key=? AND j.job_type='memory.extract' ORDER BY j.created_at,j.id",
            (scope.storage_key,))]
        turns = [dict(row) for row in connection.execute(
            "SELECT t.turn_id,t.session_id,t.status,t.error_text FROM turns t "
            "JOIN sessions s ON s.session_id=t.session_id WHERE s.scope_key=? ORDER BY t.created_at",
            (scope.storage_key,))]
        messages = [dict(row) for row in connection.execute(
            "SELECT m.message_id,m.role,m.content_text,m.turn_id FROM messages m "
            "JOIN sessions s ON s.session_id=m.session_id WHERE s.scope_key=? ORDER BY m.created_at,m.seq_id",
            (scope.storage_key,))]
        tools = [dict(json.loads(row[1]), turn_id=row[0]) for row in connection.execute(
            "SELECT t.turn_id,t.tool_call_json FROM tool_calls t JOIN sessions s ON s.session_id=t.session_id "
            "WHERE s.scope_key=? ORDER BY t.created_at", (scope.storage_key,))]
    for job in jobs:
        job["input"] = json.loads(job.pop("input_json"))
        job["result"] = json.loads(job.pop("result_json") or "null")
    return dict(jobs=jobs, turns=turns, messages=messages, tools=tools)


def _wait_for_extraction(db: Database, scope: MemoryScope, expected_turns: int, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        snapshot = _database_snapshot(db, scope)
        jobs, turns = snapshot["jobs"], snapshot["turns"]
        assigned = {identity for job in jobs for identity in job["input"].get("turn_ids", ())}
        source_turns = {turn["turn_id"] for turn in turns if turn["status"] == "completed"}
        if (len(source_turns) == expected_turns and source_turns <= assigned
            and jobs and all(job["status"] == "completed" for job in jobs)):
            snapshot["extraction_complete"] = True
            return snapshot
        if (any(job["status"] in {"terminal_failed", "cancelled"} for job in jobs)
            or time.monotonic() >= deadline):
            snapshot["extraction_complete"] = False
            return snapshot
        time.sleep(0.2)


def _read_full(tool: MemoryReadTool, scope: MemoryScope, reference: str) -> str | None:
    content, offset = [], 0
    for _ in range(32):
        result = tool.execute(dict(reference=reference, offset=offset, limit=4000), context=ToolInvocationContext(memory_scope=scope))
        if not result.ok:
            return None
        content.append(result.data["content"])
        offset = result.data["next_offset"]
        if offset is None:
            return "".join(content)
    raise RuntimeError("评测原文超过分页读取上限")


def _check_retrieval(specs, score, cards, db, scope, root) -> tuple[list[dict], dict]:
    retriever = MemoryRetriever(db)
    search, read = MemorySearchTool(retriever), MemoryReadTool(retriever)
    loader = MemoryContextLoader(root / "memory", experience_retriever=retriever)
    other = MemoryScope(tenant_id=scope.tenant_id, user_id=scope.user_id + "-other")
    checks = dict(exact_evidence=True, scope_isolation=True,
                  no_irrelevant_recall=not retriever.recall(scope, "zzunrelatedquasarxyz"))
    rows = []
    for index, spec in enumerate(specs):
        result = search.execute(dict(query=spec["query"], source="card", kind=spec["kind"]), context=ToolInvocationContext(memory_scope=scope))
        hits = result.data["hits"] if result.ok else []
        identity = score["matched"].get(index)
        reference = f"card:{identity}" if identity else None
        expected_found = reference is not None and any(hit["reference"] == reference for hit in hits)
        card_body = _read_full(read, scope, reference) if reference else None
        recalled = retriever.recall(scope, spec["query"])
        snapshot = loader.freeze(scope, session_id="new-session-eval", query=spec["query"])
        should_recall = spec["kind"] == "reusable_experience"
        recall_ok = (any(hit.reference == reference for hit in recalled)
                     and reference in (snapshot.experience_prompt or "")) if should_recall else not recalled
        foreign = search.execute(dict(query=spec["query"], source="card"), context=ToolInvocationContext(memory_scope=other))
        checks["scope_isolation"] &= bool(foreign.ok and not foreign.data["hits"])
        rows.append(dict(query=spec["query"], kind=spec["kind"], hits=hits,
                         expected_reference=reference, found_expected_card=expected_found,
                         card_readable=card_body is not None, recall_correct=bool(recall_ok),
                         automatic_references=[hit.reference for hit in recalled],
                         frozen_experience_prompt=snapshot.experience_prompt))
    for card in cards:
        checks["scope_isolation"] &= _read_full(read, other, f"card:{card['item_id']}") is None
        checks["exact_evidence"] &= bool(card["evidence"])
        for evidence in card["evidence"]:
            prefix = {"message": "message", "tool_call": "tool"}.get(evidence["source_type"])
            reference = f"{prefix}:{evidence['source_id']}" if prefix else None
            raw = _read_full(read, scope, reference) if reference else None
            quote = evidence.get("excerpt")
            checks["exact_evidence"] &= bool(raw and quote and quote in raw)
            if reference:
                checks["scope_isolation"] &= _read_full(read, other, reference) is None
    return rows, checks


def _case_sessions(case: dict) -> list[list[str]]:
    """`sessions` 按顺序表示多个会话；单会话案例仍可只写 `turns`。"""
    return case.get("sessions") or [case["turns"]]


def _run_case(case: dict, port: int, db: Database, root: Path, run_id: str, wait_seconds: float) -> dict:
    user = f"memory-eval-{run_id}-{case['id']}"
    scope = MemoryScope(tenant_id="frontier", user_id=user)
    address = dict(user_id=user, chat_id=user)
    sessions = _case_sessions(case)
    replies, sent = [], 0
    for index, turns in enumerate(sessions):
        definition = (case.get("history_definition") or [])[index:index + 1]
        occurred_at = definition[0].get("occurred_at") if definition else None
        replies.extend(_http(port, "/api/chat", dict(address, text=(
            f"对话时间：{occurred_at}\n{text}" if occurred_at else text))) for text in turns)
        sent += len(turns)
        # 真正结束旧会话；也让不足渐进间隔的尾批通过生产 reset 路径补提炼。
        _http(port, "/api/reset", address)
        snapshot = _wait_for_extraction(db, scope, sent, wait_seconds)
        if not snapshot["extraction_complete"]:
            break
    cards = [card.model_dump(mode="json") for card in SqliteMemoryStore(db).search(scope, "", limit=100)]
    score = score_cards(case["expected"], cards, strict_count=case.get("strict_card_count", True),
                        forbidden_memories=case.get("forbidden_memories", ()))
    retrieval, checks = _check_retrieval(case["expected"], score, cards, db, scope, root)
    checks["extraction_complete"] = snapshot["extraction_complete"]
    checks["dialogue_completed"] = (len(snapshot["turns"]) == sum(map(len, sessions))
                                     and all(turn["status"] == "completed" for turn in snapshot["turns"]))
    # 格式错误导致提案全被丢弃，不能伪装成正常的空结果。
    checks["no_parse_issues"] = all(not (job.get("result") or {}).get("review", {}).get("extraction_issues")
                                    for job in snapshot["jobs"])
    # 多会话案例的前序会话可以合法产卡，最终状态由 score_cards 判断。
    if not case["expected"] and len(sessions) == 1:
        checks["no_noise_proposals"] = all(not (job.get("result") or {}).get("review", {}).get("proposals")
                                           for job in snapshot["jobs"])
    probe_result = None
    if case.get("probe"):
        checks["new_session_probe"] = False
    if case.get("probe") and snapshot["extraction_complete"] and score["passed"]:
        probe = case["probe"]
        question_time = (case.get("golden_question") or {}).get("asked_at")
        probe_text = f"当前对话时间：{question_time}\n{probe['text']}" if question_time else probe["text"]
        response = _http(port, "/api/chat", dict(address, text=probe_text))
        after = _database_snapshot(db, scope)
        old_ids = {turn["turn_id"] for turn in snapshot["turns"]}
        new_turns = [turn for turn in after["turns"] if turn["turn_id"] not in old_ids]
        new_ids = {turn["turn_id"] for turn in new_turns}
        calls = [tool for tool in after["tools"] if tool["turn_id"] in new_ids]
        successful_tools = {tool["tool_name"] for tool in calls if tool["status"] == "success"}
        text = response["reply"].casefold()
        probe_checks = dict(
            new_session=bool(new_turns) and all(turn["session_id"] not in
                {old["session_id"] for old in snapshot["turns"]} for turn in new_turns),
            answer_contains_facts=all(any(term.casefold() in text for term in group) for group in probe["anchors"]),
            requested_tools_used=set(probe["tools"]) <= successful_tools,
            answer_lacks_forbidden_literals=not any(term.casefold() in text for term in probe.get("lacks", ())),
        )
        if "memory_read" in probe["tools"]:
            expected_refs = {f"card:{identity}" for identity in score["matched"].values()}
            probe_checks["read_expected_card"] = any(
                tool["tool_name"] == "memory_read" and tool["status"] == "success"
                and tool.get("arguments", {}).get("reference") in expected_refs for tool in calls)
        probe_result = dict(reply=response, tools=calls, checks=probe_checks, passed=all(probe_checks.values()))
        checks["new_session_probe"] = probe_result["passed"]
    automatic_passed = score["passed"] and all(checks.values()) and all(
        row["found_expected_card"] and row["card_readable"] and row["recall_correct"] for row in retrieval)
    review_required = bool((case.get("golden_question") or {}).get("requires_semantic_review"))
    declared_turn_ids = [turn["turn_id"] for session in case.get("history_definition", []) for turn in session["turns"]]
    real_messages = [message for message in snapshot["messages"] if message["role"] == "user"]
    return dict(id=case["id"], description=case["description"], scope=scope.model_dump(mode="json"),
                input=[text for turns in sessions for text in turns], sessions=sessions,
                expected=case["expected"], replies=replies, cards=cards,
                score=score, retrieval=retrieval, checks=checks, probe=probe_result,
                database=snapshot, automatic_checks_passed=bool(automatic_passed),
                review_status="pending" if review_required else "not_required",
                golden_question=case.get("golden_question"), case_id=case.get("case_id", case["id"]),
                case_hash=case.get("case_hash"),
                source_message_id_map=dict(zip(declared_turn_ids, (message["message_id"] for message in real_messages))),
                passed=bool(automatic_passed and not review_required))


def _save_report(report: dict, root: Path) -> None:
    cases = report["cases"]
    report["passed"] = (bool(cases) and all(case["passed"] for case in cases)
                        and report.get("database_audit_passed", True))
    report["summary"] = dict(cases=len(cases), passed=sum(case["passed"] for case in cases),
        expected_cards=sum(len(case.get("expected", [])) for case in cases),
        actual_cards=sum(case.get("score", {}).get("actual_count", 0) for case in cases),
        matched_cards=sum(case.get("score", {}).get("matched_count", 0) for case in cases),
        retrieval_expected=sum(len(case.get("expected", [])) for case in cases),
        retrieval_found=sum(row["found_expected_card"] for case in cases for row in case.get("retrieval", [])))
    report["summary"]["negative_cases"] = sum(not case.get("expected") for case in cases)
    report["summary"]["negative_cases_passed"] = sum(not case.get("expected") and case["passed"] for case in cases)
    report["summary"]["automatic_checks_passed"] = sum(case.get("automatic_checks_passed", case["passed"]) for case in cases)
    report["summary"]["needs_semantic_review"] = sum(case.get("review_status") == "pending" for case in cases)
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Memory 行为评测", "", f"模型：{report['model']}；数据库：`{root / 'agent.db'}`。",
             "", f"通过 {report['summary']['passed']}/{len(cases)} 个案例。", "",
             "| 案例 | 预期卡片 | 实际卡片 | 命中预期 | 结果 |", "|---|---:|---:|---:|---|"]
    for case in cases:
        score = case.get("score", {})
        verdict = ("待语义复核" if case.get("review_status") == "pending"
                   and case.get("automatic_checks_passed") else "通过" if case["passed"] else "失败")
        lines.append(f"| {case['id']} | {score.get('expected_count', '—')} | {score.get('actual_count', '—')} | "
                     f"{score.get('matched_count', '—')} | {verdict} |")
    for case in cases:
        lines.extend(["", f"## {case['id']}：{case['description']}", "", *[f"> {text}" for text in case.get("input", [])]])
        for card in case.get("cards", []):
            lines.append(f"- `{card['kind']}`：{card['content']}")
        if case.get("error"):
            lines.append(f"- 执行错误：{case['error']}")
        for issue in case.get("score", {}).get("issues", []):
            lines.append(f"- 提取问题：`{issue['code']}`")
        for name, passed in case.get("checks", {}).items():
            if not passed:
                lines.append(f"- 检查失败：`{name}`")
        for row in case.get("retrieval", []):
            lines.append(f"- 检索 `{row['query']}`：{'命中' if row['found_expected_card'] else '未命中'}；"
                         f"自动召回：{'符合预期' if row['recall_correct'] else '不符合预期'}。")
        if case.get("golden_question"):
            question = case["golden_question"]
            lines.extend(["", f"黄金问题：{question.get('text', '')}",
                          f"参考回答：{question['reference_answer']}",
                          "必须满足：" + "；".join(question["required_meaning"]),
                          "禁止推断：" + "；".join(question.get("forbidden_meaning", ())),
                          f"语义复核：{case['review_status']}"])
    lines.extend(["", "评测说明：通过真实 HTTP 对话和生产后台提炼执行，reset 用于新会话及补提炼。",
                  "事实内容使用预先写定的类别、关键含义和结果极性检查，不能替代全面的语义评估。",
                  "JSON 报告保留每条原文、卡片、证据、作业尝试次数、提案拒绝原因及检索结果。"])
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def audit_database(output_dir: Path) -> dict:
    """不调用模型：复核已有评测库的完整性、原文回读和实际上下文注入。"""
    root = Path(output_dir).resolve()
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    db = Database(root / "agent.db")
    retriever = MemoryRetriever(db)
    search, read = MemorySearchTool(retriever), MemoryReadTool(retriever)
    with db.read() as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = dict(connection.execute("SELECT kind,count(*) FROM memory_cards WHERE status='active' GROUP BY kind"))
    cases = []
    for case in report["cases"]:
        if "scope" not in case:
            cases.append(dict(id=case["id"], skipped="case failed before result collection"))
            continue
        scope = MemoryScope.model_validate(case["scope"])
        sources = [message for message in case["database"]["messages"] if message["role"] == "user"]
        raw_checks = []
        for message in sources:
            ref = f"message:{message['message_id']}"
            result = search.execute(dict(query=message["content_text"][:32], source="history"), context=ToolInvocationContext(memory_scope=scope))
            raw_checks.append(dict(reference=ref,
                readable=_read_full(read, scope, ref) == message["content_text"],
                searchable=result.ok and any(hit["reference"] == ref for hit in result.data["hits"])))
        raw_tools = []
        for tool in case["database"]["tools"]:
            if tool["tool_name"].startswith("memory_"):
                continue
            ref = f"tool:{tool['tool_call_id']}"
            raw_tools.append(dict(reference=ref, readable=_read_full(read, scope, ref) is not None))
        experience_ids = {card["item_id"] for card in case["cards"] if card["kind"] == "reusable_experience"}
        original_turns = {turn["turn_id"] for turn in case["database"]["turns"]}
        selected = []
        with db.read() as connection:
            for (payload,) in connection.execute(
                "SELECT t.turn_json FROM turns t JOIN sessions s ON s.session_id=t.session_id WHERE s.scope_key=?",
                (scope.storage_key,)):
                turn = json.loads(payload)
                if turn["turn_id"] in original_turns:
                    continue
                records = (turn.get("perception_trace") or {}).get("records", [])
                selected.extend(record for record in records if record["item_name"] == "memory_experience"
                                and record["decision"] == "selected")
        actual_injection = None
        if experience_ids and case.get("probe"):
            actual_injection = any(identity in record["content_preview"] for identity in experience_ids for record in selected)
        cases.append(dict(id=case["id"], messages=raw_checks, tools=raw_tools,
                          actual_experience_injection=actual_injection,
                          passed=all(row["readable"] and row["searchable"] for row in raw_checks)
                          and all(row["readable"] for row in raw_tools) and actual_injection is not False))
    audit = dict(integrity=integrity, foreign_key_errors=foreign_keys, active_cards_by_kind=counts, cases=cases,
                 passed=integrity == ["ok"] and not foreign_keys and all(case.get("passed", False) for case in cases))
    (root / "database_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def run_suite(suite: dict, output_dir: Path, *, wait_seconds: float = 180, progress=print) -> dict:
    """使用当前配置的真实模型；测试可在 ModelRegistry 这个外部边界提供 fixture。"""
    from agent.runtime import build_runtime

    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "agent.db").exists():
        raise ValueError("评测目录已存在 agent.db，请使用新的目录，以免历史记忆污染结果")
    registry = ModelRegistry.default()
    registry.register(_RecordedModel(registry.get(), root / "model_calls.jsonl"), as_default=True)
    runtime = build_runtime(RuntimeConfig(data_root=root), model_registry=registry)
    server = None
    report = dict(suite=suite["name"], suite_version=suite["version"],
                  started_at=datetime.now(timezone.utc).isoformat(),
                  model=getattr(runtime.gateway.loop.model, "model_name", runtime.gateway.loop.model.name),
                  provider=runtime.gateway.loop.model.name, extractor_version=MemoryExtractor.version, cases=[])
    for name in ("dataset_id", "dataset_version", "dataset_hash", "schema_version"):
        if name in suite:
            report[name] = suite[name]
    try:
        runtime.start()
        server = FrontierHttpServer(runtime.gateway, host="127.0.0.1", port=0, enable_memory_debug=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        report["backend_url"] = f"http://127.0.0.1:{port}"
        report["backend_status"] = _http(port, "/api/status")
        progress(f"Backend ready: {report['backend_url']}; model={report['model']}; database={root / 'agent.db'}")
        db, run_id = Database(root / "agent.db"), uuid.uuid4().hex[:8]
        for case in suite["cases"]:
            progress(f"RUN {case['id']}: {case['description']}")
            try:
                result = _run_case(case, port, db, root, run_id, wait_seconds)
            except Exception as exc:
                scope = MemoryScope(tenant_id="frontier", user_id=f"memory-eval-{run_id}-{case['id']}")
                result = dict(id=case["id"], description=case["description"],
                              input=[text for turns in _case_sessions(case) for text in turns],
                              expected=case["expected"], passed=False, error=f"{type(exc).__name__}: {exc}",
                              database=_database_snapshot(db, scope))
            report["cases"].append(result)
            _save_report(report, root)
            verdict = "REVIEW" if result.get("review_status") == "pending" and result.get("automatic_checks_passed") else "PASS" if result["passed"] else "FAIL"
            progress(f"{verdict} {case['id']} {result.get('score', {}).get('issues', result.get('error', ''))}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        runtime.stop()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _save_report(report, root)
    report["database_audit_passed"] = audit_database(root)["passed"]
    _save_report(report, root)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立 HTTP 后端，使用真实 LLM 评测 Memory")
    parser.add_argument("--live", action="store_true", help="明确启用当前 .env 配置的真实模型调用")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2] / "tests/eval/datasets/memory_behavior.json")
    parser.add_argument("--dataset-db", type=Path, help="从数据库加载已发布案例")
    parser.add_argument("--dataset-version", help="案例集内容版本，如 1.0.0 或 2.0.0")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="*", help="只运行指定案例 ID")
    parser.add_argument("--wait-seconds", type=float, default=180)
    args = parser.parse_args()
    if not args.live:
        parser.error("真实模型评测需指定 --live；离线测试请运行 pytest")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.output_dir / "backend.log", level=logging.WARNING, encoding="utf-8")
    from .datasets.memory_dataset import to_behavior_suite
    from agent.types.eval.datasets import MemoryEvalDataset
    if args.dataset_db or args.dataset_version:
        if not args.dataset_db or not args.dataset_version:
            parser.error("--dataset-db 和 --dataset-version 必须一起提供")
        from ..storage.sqlite.memory_eval_store import SqliteMemoryEvalStore
        definition = SqliteMemoryEvalStore(Database(args.dataset_db)).get_dataset("memory_behavior", args.dataset_version)
        if definition is None:
            parser.error("数据库中没有指定案例版本")
        suite = to_behavior_suite(definition)
    else:
        raw = json.loads(args.dataset.read_text(encoding="utf-8"))
        suite = to_behavior_suite(MemoryEvalDataset.model_validate(raw)) if "dataset_id" in raw else raw
    if args.cases:
        unknown = set(args.cases) - {identity for case in suite["cases"] for identity in (case["id"], case.get("case_id", case["id"]))}
        if unknown:
            parser.error(f"未知案例：{sorted(unknown)}")
        suite["cases"] = [case for case in suite["cases"] if case["id"] in args.cases or case.get("case_id") in args.cases]
    report = run_suite(suite, args.output_dir, wait_seconds=args.wait_seconds)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["passed"] else 2 if report["summary"]["needs_semantic_review"] and all(
        case.get("automatic_checks_passed") for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
