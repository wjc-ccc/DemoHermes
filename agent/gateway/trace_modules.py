"""已落库 trace 的只读适配器。页面只认识这一种形状，加模式只注册适配器。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..storage.sqlite.catalog import CatalogError, open_readonly
from ..storage.sqlite.trace_store import SqliteTraceStore
from ..storage.sqlite.database import Database

_PRECISION_NOTE = (
    "单轮指标已写入 turns.payload_json。"
    "滚动窗口（p95 / p99 / P3 命中率）只在当前进程，重启后丢失。"
)


def list_modules() -> dict:
    return {
        "modules": [
            {
                "module": spec["module"],
                "label": spec["label"],
                "persisted": spec["persisted"],
                "note": spec["note"],
            }
            for spec in _MODULES
        ]
    }


def list_traces(
    databases: Mapping[str, Path],
    module: str,
    *,
    session_id: str | None,
    limit: int,
    offset: int,
    filters: Mapping[str, object] | None = None,
) -> dict:
    spec = next((item for item in _MODULES if item["module"] == module), None)
    if spec is None:
        raise CatalogError("unknown module")
    bounded = max(1, min(int(limit), 100))
    start = max(0, int(offset))
    options = _trace_filter_options(databases, module, session_id)
    selected = dict(filters or {})
    if any(name not in options for name in selected):
        raise CatalogError("unknown filter column")
    if any(value not in options[name] for name, value in selected.items()):
        raise CatalogError("unknown filter value")
    columns, rows, total = spec["read"](
        databases, session_id=session_id or None, limit=bounded, offset=start,
        filters=selected,
    )
    return {
        "module": spec["module"],
        "persisted": spec["persisted"],
        "note": spec["note"],
        "columns": [{"name": name} for name in columns],
        "filter_options": options,
        "rows": rows,
        "total": total,
        "limit": bounded,
        "offset": start,
    }


_PRECISION_COLUMNS = (
    "trace_id",
    "session_id",
    "turn_id",
    "created_at",
    "conversation",
    "budget",
    "tokens_used",
    "budget_usage",
    "message_count",
    "total_chars",
    "dropped_count",
    "deferred_count",
    "compressed_count",
    "p0_dropped_count",
    "p1_dropped_count",
    "protected_error_missing_count",
    "p3_exposed_count",
    "p0_over_share",
    "anchor_status",
    "records",
    "exposed_refs",
    "p3_hit_refs",
)

_MEMORY_COLUMNS = (
    "trace_id",
    "session_id",
    "turn_id",
    "task_id",
    "job_id",
    "operation",
    "validator_version",
    "latency_ms",
    "created_at",
    "conversation",
    "items",
)


def _trace_filter_options(
    databases: Mapping[str, Path], module: str, session_id: str | None
) -> dict[str, list[object]]:
    path = _memory_file(databases)
    if path is None:
        return {}
    conn = open_readonly(path)
    try:
        if module == "memory":
            if not _has_table(conn, "traces"):
                return {}
            values = [row[0] for row in conn.execute(
                "SELECT DISTINCT operation FROM traces WHERE module='memory' "
                "AND (? IS NULL OR session_id=?) ORDER BY operation LIMIT 41",
                (session_id, session_id),
            )]
            return {"operation": values} if 0 < len(values) <= 40 else {}
        if not _has_table(conn, "turns"):
            return {}
        expression = "json_extract(payload_json,'$.precision_trace.metrics.anchor_status')"
        values = [row[0] for row in conn.execute(
            f"SELECT DISTINCT {expression} FROM turns WHERE "
            "(? IS NULL OR session_id=?) AND "
            "json_type(payload_json,'$.precision_trace')='object' AND "
            f"{expression} IS NOT NULL ORDER BY {expression} LIMIT 41",
            (session_id, session_id),
        )]
        return {"anchor_status": values} if 0 < len(values) <= 40 else {}
    finally:
        conn.close()


def _read_precision(
    databases: Mapping[str, Path], *, session_id: str | None, limit: int, offset: int,
    filters: Mapping[str, object],
) -> tuple[list[str], list[dict]]:
    path = _memory_file(databases)
    if path is None:
        return list(_PRECISION_COLUMNS), [], 0
    conn = open_readonly(path)
    try:
        if not _has_table(conn, "turns"):
            return list(_PRECISION_COLUMNS), [], 0
        conn.execute("BEGIN")
        where = " WHERE (? IS NULL OR session_id=?) AND json_type(payload_json,'$.precision_trace')='object'"
        params: tuple[object, ...] = (session_id, session_id)
        if "anchor_status" in filters:
            where += " AND json_extract(payload_json,'$.precision_trace.metrics.anchor_status') IS ?"
            params += (filters["anchor_status"],)
        total = conn.execute("SELECT COUNT(*) FROM turns"+where, params).fetchone()[0]
        stored = conn.execute(
            "SELECT turn_id, session_id, created_at, payload_json FROM turns "
            + where + " ORDER BY created_at DESC,turn_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        matched = []
        for turn_id, row_session_id, created_at, payload_json in stored:
            trace = _precision_payload(payload_json)
            if trace is not None:
                matched.append((turn_id, row_session_id, created_at, trace))
        talks = _conversations(conn, [item[0] for item in matched])
    finally:
        conn.close()
    rows = [
        _precision_row(turn_id, row_session_id, created_at, trace, talks.get(turn_id, ""))
        for turn_id, row_session_id, created_at, trace in matched
    ]
    return _columns(_PRECISION_COLUMNS, rows), rows, total


def _read_memory(
    databases: Mapping[str, Path], *, session_id: str | None, limit: int, offset: int,
    filters: Mapping[str, object],
) -> tuple[list[str], list[dict]]:
    path = _memory_file(databases)
    if path is None:
        return list(_MEMORY_COLUMNS), [], 0
    conn = open_readonly(path)
    try:
        conn.execute("BEGIN")
        where = " WHERE module='memory' AND (? IS NULL OR session_id=?)"
        params: tuple[object, ...] = (session_id, session_id)
        if "operation" in filters:
            where += " AND operation IS ?"
            params += (filters["operation"],)
        total=conn.execute("SELECT COUNT(*) FROM traces"+where,params).fetchone()[0]
        ids=conn.execute("SELECT id FROM traces"+where+" ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                         (*params,limit,offset)).fetchall()
        traces=[SqliteTraceStore._get_with_conn(conn,row[0]) for row in ids]
        talks = _conversations(conn, [trace.turn_id for trace in traces if trace.turn_id])
    finally:
        conn.close()
    rows = [_memory_row(trace, talks.get(trace.turn_id or "", "")) for trace in traces]
    return _columns(_MEMORY_COLUMNS, rows), rows, total


def _precision_row(
    turn_id: str, session_id: str, created_at: object, trace: dict, conversation: str
) -> dict:
    metrics = trace.get("metrics") if isinstance(trace.get("metrics"), dict) else {}
    row = {
        "trace_id": str(metrics.get("trace_id") or turn_id),
        "session_id": session_id,
        "turn_id": turn_id,
        "created_at": None if created_at is None else str(created_at),
        "conversation": conversation,
        "records": _json_cell(trace.get("records") or []),
        "exposed_refs": _json_cell(trace.get("exposed_refs") or []),
        "p3_hit_refs": _json_cell(trace.get("p3_hit_refs") or []),
    }
    for key, value in metrics.items():
        if key != "trace_id":
            row[key] = _json_cell(value)
    return row


def _memory_row(trace, conversation: str) -> dict:
    return {
        "trace_id": trace.trace_id,
        "session_id": trace.session_id,
        "turn_id": trace.turn_id,
        "task_id": trace.task_id,
        "job_id": trace.job_id,
        "operation": trace.operation.value,
        "validator_version": trace.validator_version,
        "latency_ms": trace.latency_ms,
        "created_at": trace.created_at.isoformat(),
        "conversation": conversation,
        "items": _json_cell([
            {
                "trace_item_id": item.trace_item_id,
                "item_id": item.item_id,
                "proposal_id": item.proposal_id,
                "decision": item.decision.value,
                "type": item.type,
                "layer": item.layer.value if item.layer else None,
                "source_type": item.source_type,
                "source_ref": item.source_ref,
                "validation_codes": list(item.runtime.validation_codes),
            }
            for item in trace.items
        ]),
    }


def _conversations(conn, turn_ids: list[str]) -> dict[str, str]:
    ids = [turn_id for turn_id in turn_ids if turn_id]
    if not ids or not _has_table(conn, "messages"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT turn_id, role, content_text FROM messages "
        f"WHERE turn_id IN ({placeholders}) ORDER BY seq_id",
        ids,
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for turn_id, role, text in rows:
        grouped.setdefault(turn_id, []).append(f"{role}: {text}")
    return {turn_id: "\n".join(parts) for turn_id, parts in grouped.items()}


def _columns(preferred: tuple[str, ...], rows: list[dict]) -> list[str]:
    extra = sorted({key for row in rows for key in row} - set(preferred))
    return [*preferred, *extra]


def _json_cell(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _precision_payload(payload_json: str) -> dict | None:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    trace = payload.get("precision_trace") if isinstance(payload, dict) else None
    return trace if isinstance(trace, dict) else None


def _memory_file(databases: Mapping[str, Path]) -> Path | None:
    path = databases.get("agent")
    if path is None or not Path(path).is_file():
        return None
    return Path(path)


def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


_MODULES = (
    {
        "module": "precision",
        "label": "Precision",
        "persisted": True,
        "note": _PRECISION_NOTE,
        "read": _read_precision,
    },
    {
        "module": "memory",
        "label": "Memory",
        "persisted": True,
        "note": None,
        "read": _read_memory,
    },
)
