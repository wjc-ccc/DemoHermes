-- 统计是当前正文代际和主模型的缓存，更新统计不会推进卡片修订号。
ALTER TABLE memory_cards ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count>=0);
ALTER TABLE memory_cards ADD COLUMN judge_count INTEGER NOT NULL DEFAULT 0 CHECK(judge_count>=0 AND judge_count<=used_count);
ALTER TABLE memory_cards ADD COLUMN effective_count INTEGER NOT NULL DEFAULT 0 CHECK(effective_count>=0 AND effective_count<=judge_count);
ALTER TABLE memory_cards ADD COLUMN helpful_count INTEGER NOT NULL DEFAULT 0 CHECK(helpful_count>=0 AND helpful_count<=effective_count);
ALTER TABLE memory_cards ADD COLUMN harmful_count INTEGER NOT NULL DEFAULT 0 CHECK(harmful_count>=0 AND helpful_count+harmful_count<=effective_count);
ALTER TABLE memory_cards ADD COLUMN recall_enabled INTEGER NOT NULL DEFAULT 1 CHECK(recall_enabled IN (0,1));

CREATE TABLE memory_card_decisions (
 id TEXT PRIMARY KEY,
 scope_key TEXT NOT NULL,
 card_id TEXT NOT NULL REFERENCES memory_cards(id),
 card_revision INTEGER NOT NULL CHECK(card_revision>=1),
 decision_type TEXT NOT NULL CHECK(decision_type IN ('usage','merge')),
 turn_id TEXT,
 model_key TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 dedupe_key TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL,
 judged_at INTEGER,
 counted_at INTEGER,
 CHECK(decision_type<>'usage' OR turn_id IS NOT NULL),
 CHECK(counted_at IS NULL OR judged_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_memory_card_usage ON memory_card_decisions(card_id,card_revision,turn_id,model_key)
 WHERE decision_type='usage';
CREATE INDEX idx_memory_card_decisions_pending ON memory_card_decisions(decision_type,judged_at,created_at);
CREATE INDEX idx_memory_card_decisions_model ON memory_card_decisions(model_key,card_id,card_revision);
CREATE INDEX idx_memory_card_decisions_turn ON memory_card_decisions(turn_id,model_key);
