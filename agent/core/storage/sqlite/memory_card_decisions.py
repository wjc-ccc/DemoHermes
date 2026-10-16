"""经验曝光及判官决策的唯一持久记录，卡片统计只是可重建缓存。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from agent.core.memory.config import MemoryExperienceConfig
from agent.types.memory.categories import UserCardKind
from agent.types.memory.decisions import DecisionItem, UsageJudgmentItem

from .codec import dumps, milliseconds
from .errors import IntegrityError, RevisionConflictError
from .memory_store import read_user_memory


_COUNTS = ("used_count", "judge_count", "effective_count", "helpful_count", "harmful_count")


def _read(conn, identity: str) -> DecisionItem | None:
    cursor = conn.execute("SELECT * FROM memory_card_decisions WHERE id=?", (identity,))
    row = cursor.fetchone()
    if row is None:
        return None
    result = dict(zip((column[0] for column in cursor.description), row))
    result["result_json"] = json.loads(result["result_json"])
    judgment = result["result_json"].get("judgment") or {}
    for key in ("judge_key", "adopted", "effect", "reason"):
        result[key] = judgment.get(key)
    result["evidence_refs"] = tuple(judgment.get("evidence_refs") or ())
    return result


def _generation_matches(card, decision) -> bool:
    return (card is not None and card.kind is UserCardKind.REUSABLE_EXPERIENCE
            and int(card.metadata.get("stats_generation_start", 1)) <= decision["card_revision"] <= card.revision)


def _contribution(adopted, effect, *, judged: bool) -> tuple[int, int, int, int]:
    if not judged:
        return (0, 0, 0, 0)
    effective = int(adopted == "yes")
    return (1, effective, int(effective and effect == "helpful"), int(effective and effect == "harmful"))


class SqliteMemoryCardDecisions:
    """同一曝光只计一次；复评在同一事务中扣除旧贡献再应用新贡献。"""

    def __init__(self, db, config: MemoryExperienceConfig | None = None):
        self.db = db
        self.config = config or MemoryExperienceConfig()

    def get(self, identity: str) -> DecisionItem | None:
        with self.db.read() as conn:
            return _read(conn, identity)

    def get_by_key(self, dedupe_key: str) -> DecisionItem | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT id FROM memory_card_decisions WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            return _read(conn, row[0]) if row else None

    def record_decision(self, conn, *, scope, card_id: str, card_revision: int,
                        decision_type: str, model_key: str, dedupe_key: str,
                        context: dict, result: dict | None = None, judge_key: str = "") -> dict:
        if decision_type != "merge":
            raise ValueError("非使用决策必须是 merge，关系阶段由 context.stage 区分")
        card = read_user_memory(conn, card_id, scope_key=scope.storage_key)
        if card is None:
            raise IntegrityError("decision card outside scope")
        existing = conn.execute("SELECT id FROM memory_card_decisions WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if existing:
            item = _read(conn, existing[0])
            if item["scope_key"] != scope.storage_key:
                raise IntegrityError("decision key outside scope")
            return {**item, "inserted": False}
        identity = str(uuid.uuid4())
        now = milliseconds()
        payload = {"context": context}
        if result is not None:
            payload["judgment"] = {**result, "judge_key": judge_key or None,
                                   "evidence_refs": list(result.get("evidence_refs") or ())}
        conn.execute("INSERT INTO memory_card_decisions "
                     "(id,scope_key,card_id,card_revision,decision_type,model_key,result_json,"
                     "dedupe_key,created_at,judged_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (identity, scope.storage_key, card_id, card_revision, decision_type, model_key,
                      dumps(payload), dedupe_key, now, now if result is not None else None))
        return {**_read(conn, identity), "inserted": True}

    def complete_decision(self, conn, identity: str, result: dict, *, judge_key: str = "",
                          evidence_refs=()) -> DecisionItem:
        item = _read(conn, identity)
        if item is None or item["decision_type"] == "usage":
            raise ValueError("非使用决策不存在")
        judgment = {**result, "judge_key": judge_key or None, "evidence_refs": list(evidence_refs)}
        if item["judged_at"] is not None:
            if item["result_json"].get("judgment") != judgment:
                raise RevisionConflictError("decision already completed")
            return item
        payload = {**item["result_json"], "judgment": judgment}
        conn.execute("UPDATE memory_card_decisions SET result_json=?,judged_at=? "
                     "WHERE id=? AND judged_at IS NULL",
                     (dumps(payload), milliseconds(), identity))
        return _read(conn, identity)

    def record_exposure(self, conn, *, scope, card_id: str, card_revision: int, turn_id: str,
                        model_key: str, card_content: str | None = None,
                        context: dict | None = None) -> str | None:
        card = read_user_memory(conn, card_id, scope_key=scope.storage_key)
        if card is None or card.kind is not UserCardKind.REUSABLE_EXPERIENCE:
            return None
        if card_revision > card.revision:
            return None
        known_turn = conn.execute("SELECT s.scope_key FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                                  "WHERE t.turn_id=?", (turn_id,)).fetchone()
        if known_turn and known_turn[0] != scope.storage_key:
            raise IntegrityError("exposure turn outside scope")
        generation = int(card.metadata.get("stats_generation_start", 1))
        if generation <= card_revision:
            # 同一正文代际中，补证据或向量造成的修订不能把同轮曝光重复计数。
            row = conn.execute("SELECT id FROM memory_card_decisions WHERE decision_type='usage' "
                               "AND card_id=? AND turn_id=? AND model_key=? AND card_revision BETWEEN ? AND ? "
                               "ORDER BY created_at,id LIMIT 1",
                               (card_id, turn_id, model_key, generation, card.revision)).fetchone()
        else:
            row = conn.execute("SELECT id FROM memory_card_decisions WHERE decision_type='usage' "
                               "AND card_id=? AND card_revision=? AND turn_id=? AND model_key=?",
                               (card_id, card_revision, turn_id, model_key)).fetchone()
        if row:
            return row[0]
        snapshot = card.model_dump(mode="json")
        if card_revision != card.revision:
            previous = conn.execute("SELECT snapshot_json FROM memory_card_versions WHERE card_id=? AND revision=?",
                                    (card_id, card_revision)).fetchone()
            if previous is None:
                return None
            snapshot = json.loads(previous[0])
        actual_context = {**dict(context or {}), "scope": scope.model_dump(mode="json"),
                          "card": snapshot, "card_content": card_content if card_content is not None else snapshot["content"]}
        identity = str(uuid.uuid4())
        dedupe_key = "usage:" + dumps([card_id, card_revision, turn_id, model_key])
        conn.execute("INSERT INTO memory_card_decisions "
                     "(id,scope_key,card_id,card_revision,decision_type,turn_id,model_key,result_json,dedupe_key,created_at) "
                     "VALUES (?,?,?,?, 'usage',?,?,?,?,?)",
                     (identity, scope.storage_key, card_id, card_revision, turn_id, model_key,
                      dumps({"context": actual_context}), dedupe_key, milliseconds()))
        decision = {"card_revision": card_revision}
        if _generation_matches(card, decision):
            metadata = dict(card.metadata)
            if "stats_model_key" not in metadata:
                metadata["stats_model_key"] = model_key
            if metadata["stats_model_key"] == model_key:
                conn.execute("UPDATE memory_cards SET used_count=used_count+1,metadata_json=? WHERE id=? AND revision=?",
                             (dumps(metadata), card_id, card.revision))
        return identity

    def pending_usage(self, *, limit: int = 3, turn_id: str | None = None,
                      model_key: str | None = None, scope_key: str | None = None) -> tuple[DecisionItem, ...]:
        # 曝光可先于轮次提交，后台只消费已经持久化的终态轮次，失败与中断同样保留。
        where = ["d.decision_type='usage'", "d.judged_at IS NULL",
                 "t.status IN ('completed','failed','interrupted')", "s.scope_key=d.scope_key"]
        args: list[Any] = []
        for column, value in (("d.turn_id", turn_id), ("d.model_key", model_key), ("d.scope_key", scope_key)):
            if value is not None:
                where.append(column + "=?")
                args.append(value)
        args.append(max(1, min(limit, 100)))
        with self.db.read() as conn:
            rows = conn.execute("SELECT d.id FROM memory_card_decisions d JOIN turns t ON t.turn_id=d.turn_id "
                                "JOIN sessions s ON s.session_id=t.session_id WHERE " + " AND ".join(where)
                                + " ORDER BY d.created_at,d.id LIMIT ?", args).fetchall()
            return tuple(_read(conn, row[0]) for row in rows)

    def judge_usage(self, decision_id: str, result: UsageJudgmentItem | dict, *,
                    judge_key: str = "experience-judge-v1", evidence_refs=(),
                    expected_judged_at: int | None = None) -> DecisionItem:
        if isinstance(result, UsageJudgmentItem):
            judgment = result
        else:
            judgment = UsageJudgmentItem.model_validate(result)
        refs = tuple(dict.fromkeys(evidence_refs or judgment.evidence_refs))
        if judgment.adopted == "yes" and not refs:
            raise ValueError("确认采纳必须引用本轮真实消息或工具事实")
        canonical = {**judgment.model_copy(update={"evidence_refs": refs}).model_dump(mode="json"),
                     "judge_key": judge_key}
        with self.db.write() as conn:
            item = _read(conn, decision_id)
            if item is None or item["decision_type"] != "usage":
                raise ValueError("usage decision missing")
            if (item["judged_at"] is not None and item["result_json"].get("judgment") == canonical
                    and item["judge_key"] == judge_key):
                return item
            if item["judged_at"] != expected_judged_at:
                raise RevisionConflictError("decision judgment changed")
            self._validate_evidence(conn, item, refs)
            now = max(milliseconds(), (item["judged_at"] or 0) + 1)
            card = read_user_memory(conn, item["card_id"], scope_key=item["scope_key"])
            applicable = (_generation_matches(card, item)
                          and card.metadata.get("stats_model_key") == item["model_key"])
            counted_at = None
            if applicable:
                old = _contribution(item["adopted"], item["effect"], judged=item["counted_at"] is not None)
                new = _contribution(judgment.adopted, judgment.effect, judged=True)
                counts = {name: getattr(card, name) + incoming - outgoing
                          for name, outgoing, incoming in zip(_COUNTS[1:], old, new)}
                self._write_counts(conn, card, counts)
                counted_at = now
            payload = {**item["result_json"], "judgment": canonical}
            conn.execute("UPDATE memory_card_decisions SET result_json=?,judged_at=?,counted_at=? WHERE id=?",
                         (dumps(payload), now, counted_at, decision_id))
            return _read(conn, decision_id)

    @staticmethod
    def _validate_evidence(conn, item, refs):
        # 引用只能来自被评判轮次及用户空间，provider 的关联号不作为事实 ID。
        turn = conn.execute("SELECT s.scope_key FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                            "WHERE t.turn_id=? AND t.status IN ('completed','failed','interrupted')",
                            (item["turn_id"],)).fetchone()
        if turn is None or turn[0] != item["scope_key"]:
            raise IntegrityError("usage turn not committed in scope")
        for reference in refs:
            prefix, _, identity = reference.partition(":")
            if prefix not in {"message", "tool"} or not identity:
                raise ValueError("判定证据必须使用 message: 或 tool: 引用")
            table, column = ("messages", "message_id") if prefix == "message" else ("tool_calls", "tool_call_id")
            row = conn.execute("SELECT 1 FROM " + table + " WHERE " + column + "=? AND turn_id=?",
                               (identity, item["turn_id"])).fetchone()
            if row is None:
                raise IntegrityError("judgment evidence outside turn")

    def _write_counts(self, conn, card, counts: dict, *, metadata: dict | None = None):
        metadata = dict(card.metadata) if metadata is None else metadata
        h, d = counts.get("helpful_count", card.helpful_count), counts.get("harmful_count", card.harmful_count)
        maturity = (h - d) / (h + d + self.config.maturity_prior)
        reason = None
        if h + d >= self.config.minimum_decisive_count and maturity <= self.config.disable_maturity_threshold:
            reason = "harmful_feedback"
        if metadata.get("merged_from"):
            decisive = h + d
            judged = counts.get("judge_count", card.judge_count)
            used = counts.get("used_count", card.used_count)
            if ((decisive >= self.config.merge_minimum_decisive_count and maturity < self.config.merge_accept_maturity)
                    or (used >= self.config.merge_trial_limit and judged >= self.config.merge_trial_limit
                        and decisive < self.config.merge_minimum_decisive_count)):
                reason = "merge_trial_failed"
        recall = card.recall_enabled
        if reason:
            # 显式禁用不能被后续复评或主模型切换误恢复。
            if recall or metadata.get("recall_disable_reason") in {"harmful_feedback", "merge_trial_failed"}:
                metadata["recall_disable_reason"] = reason
                metadata["recall_disable_model_key"] = metadata.get("stats_model_key")
            recall = False
        elif metadata.get("recall_disable_reason") in {"harmful_feedback", "merge_trial_failed"}:
            metadata.pop("recall_disable_reason", None)
            metadata.pop("recall_disable_model_key", None)
            now = milliseconds()
            recall = (card.status.value == "active"
                      and (card.valid_from is None or card.valid_from.timestamp() * 1000 <= now)
                      and (card.valid_until is None or card.valid_until.timestamp() * 1000 > now))
        assignments = [name + "=?" for name in counts]
        cursor = conn.execute("UPDATE memory_cards SET " + ",".join(assignments)
                              + ",recall_enabled=?,metadata_json=? WHERE id=? AND revision=?",
                              (*counts.values(), int(recall), dumps(metadata), card.item_id, card.revision))
        if cursor.rowcount != 1:
            raise RevisionConflictError("memory revision changed")

    def rebuild_stats(self, model_key: str) -> int:
        rebuilt = 0
        with self.db.write() as conn:
            ids = tuple(row[0] for row in conn.execute("SELECT id FROM memory_cards WHERE kind='reusable_experience'"))
            for identity in ids:
                card = read_user_memory(conn, identity)
                start = int(card.metadata.get("stats_generation_start", 1))
                rows = conn.execute("SELECT id FROM memory_card_decisions "
                                    "WHERE decision_type='usage' AND card_id=? AND model_key=? "
                                    "AND card_revision BETWEEN ? AND ?",
                                    (identity, model_key, start, card.revision)).fetchall()
                counts = dict.fromkeys(_COUNTS, 0)
                counts["used_count"] = len(rows)
                now = milliseconds()
                for row in rows:
                    decision = _read(conn, row[0])
                    for name, value in zip(_COUNTS[1:], _contribution(decision["adopted"], decision["effect"],
                                                                  judged=decision["judged_at"] is not None)):
                        counts[name] += value
                    if decision["judged_at"] is not None:
                        conn.execute("UPDATE memory_card_decisions SET counted_at=? WHERE id=?", (now, row[0]))
                metadata = {**card.metadata, "stats_model_key": model_key}
                self._write_counts(conn, card, counts, metadata=metadata)
                rebuilt += 1
        return rebuilt
