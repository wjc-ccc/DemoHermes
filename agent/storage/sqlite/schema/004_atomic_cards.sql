-- Atomic user cards. Old card tables are copied, then removed in this transaction.
CREATE TABLE memory_cards (
 id TEXT PRIMARY KEY,
 scope_key TEXT NOT NULL, organization_id TEXT, tenant_id TEXT, user_id TEXT, project_id TEXT,
 kind TEXT NOT NULL CHECK(kind IN ('profile','stable_preference','ongoing_fact','reusable_experience')),
 slot_key TEXT,
 title TEXT, summary TEXT NOT NULL, content TEXT NOT NULL,
 source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
 status TEXT NOT NULL CHECK(status IN ('active','superseded','deleted','expired','conflicted','quarantined')),
 priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
 confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
 source_authority TEXT NOT NULL,
 valid_from INTEGER, valid_until INTEGER,
 supersedes_card_id TEXT REFERENCES memory_cards(id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 content_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_confirmed_at INTEGER,
 CHECK((kind='reusable_experience' AND slot_key IS NULL) OR
       (kind<>'reusable_experience' AND slot_key IS NOT NULL))
);
CREATE INDEX idx_memory_cards_scope ON memory_cards(scope_key,status,kind,updated_at);
CREATE INDEX idx_memory_cards_slot ON memory_cards(scope_key,slot_key,updated_at);

CREATE TABLE memory_experiences (
 card_id TEXT PRIMARY KEY REFERENCES memory_cards(id) ON DELETE CASCADE,
 applicability TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL,
 limitations TEXT NOT NULL,
 outcome_status TEXT NOT NULL CHECK(outcome_status IN ('success','failure','mixed','observed'))
);
CREATE TABLE memory_evidence (
 card_id TEXT NOT NULL REFERENCES memory_cards(id) ON DELETE CASCADE,
 source_type TEXT NOT NULL, source_id TEXT NOT NULL,
 source_revision TEXT, source_path TEXT, source_hash TEXT,
 excerpt TEXT, observed_at INTEGER,
 PRIMARY KEY(card_id,source_type,source_id)
);
CREATE INDEX idx_memory_evidence_source ON memory_evidence(source_type,source_id);
CREATE TABLE memory_terms (
 card_id TEXT NOT NULL REFERENCES memory_cards(id) ON DELETE CASCADE,
 term TEXT NOT NULL, normalized_term TEXT NOT NULL,
 PRIMARY KEY(card_id,normalized_term)
);
CREATE INDEX idx_memory_terms_lookup ON memory_terms(normalized_term,card_id);
CREATE TABLE memory_card_versions (
 card_id TEXT NOT NULL REFERENCES memory_cards(id), revision INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), changed_at INTEGER NOT NULL,
 PRIMARY KEY(card_id,revision)
);
CREATE TABLE memory_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL, slot_key TEXT NOT NULL,
 incumbent_id TEXT REFERENCES memory_cards(id), candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
 reason TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX idx_memory_conflicts_scope ON memory_conflicts(scope_key,slot_key,created_at);

INSERT INTO memory_cards (
 id,scope_key,organization_id,tenant_id,user_id,project_id,kind,slot_key,
 title,summary,content,source_type,source_ref,metadata_json,status,priority,
 confidence,source_authority,valid_from,valid_until,supersedes_card_id,revision,
 content_hash,token_count,created_at,updated_at,last_confirmed_at
)
SELECT m.id,m.scope_key,m.organization_id,m.tenant_id,m.user_id,m.project_id,m.category,
 CASE WHEN m.category='reusable_experience' THEN NULL
      WHEN m.normalized_key IN ('preference.response_language','user.response.language','language.preference')
       THEN 'stable_preference:assistant:response_language'
      WHEN m.normalized_key IN ('preference.response_detail','user.response.detail','response.detail')
       THEN 'stable_preference:assistant:response_detail'
      WHEN m.normalized_key='identity.display_name' THEN 'profile:user:display_name'
      ELSE 'legacy:'||m.category||':'||m.normalized_key END,
 m.title,m.summary,json_extract(m.content_json,'$.content'),
 coalesce(json_extract(m.content_json,'$.source_type'),'memory_extraction'),
 coalesce(json_extract(m.content_json,'$.source_ref'),''),
 coalesce(json_extract(m.content_json,'$.metadata'),'{}'),
 CASE WHEN m.status IN ('proposed','rejected') THEN 'quarantined'
      WHEN m.status='active' AND m.category<>'reusable_experience' AND EXISTS (
       SELECT 1 FROM memory_items newer WHERE newer.scope_key=m.scope_key
        AND newer.category=m.category AND newer.status='active'
        AND (newer.normalized_key=m.normalized_key OR
         (m.normalized_key IN ('preference.response_language','user.response.language','language.preference')
          AND newer.normalized_key IN ('preference.response_language','user.response.language','language.preference')) OR
         (m.normalized_key IN ('preference.response_detail','user.response.detail','response.detail')
          AND newer.normalized_key IN ('preference.response_detail','user.response.detail','response.detail')))
        AND (newer.updated_at>m.updated_at OR
             (newer.updated_at=m.updated_at AND newer.id>m.id))) THEN 'conflicted'
      ELSE m.status END,
 m.priority,m.confidence,m.source_authority,m.valid_from,m.valid_until,
 NULL,m.revision,m.content_hash,m.token_count,
 m.created_at,m.updated_at,m.last_confirmed_at
FROM memory_items m;
UPDATE memory_cards SET supersedes_card_id=(
 SELECT supersedes_item_id FROM memory_items WHERE id=memory_cards.id)
WHERE id IN (SELECT id FROM memory_items WHERE supersedes_item_id IS NOT NULL);

INSERT INTO memory_experiences(card_id,applicability,action,outcome,limitations,outcome_status)
SELECT id,coalesce(json_extract(content_json,'$.applicability'),''),
 coalesce(json_extract(content_json,'$.action'),''),
 coalesce(json_extract(content_json,'$.outcome'),''),
 coalesce(json_extract(content_json,'$.limitations'),''),
 'observed' FROM memory_items WHERE category='reusable_experience';

INSERT OR IGNORE INTO memory_terms(card_id,term,normalized_term)
SELECT m.id,j.value,py_casefold(trim(j.value))
FROM memory_items m,json_each(m.keywords_json) j
WHERE j.type='text' AND trim(j.value)<>'';

INSERT OR IGNORE INTO memory_evidence(
 card_id,source_type,source_id,source_revision,source_path,source_hash,excerpt,observed_at)
SELECT m.id,json_extract(j.value,'$.source_type'),json_extract(j.value,'$.source_id'),
 json_extract(j.value,'$.source_revision'),json_extract(j.value,'$.source_path'),
 json_extract(j.value,'$.source_hash'),json_extract(j.value,'$.excerpt'),
 CASE WHEN json_extract(j.value,'$.observed_at') IS NULL THEN NULL
      ELSE CAST((julianday(json_extract(j.value,'$.observed_at'))-2440587.5)*86400000 AS INTEGER) END
FROM memory_items m,json_each(m.evidence_json) j
WHERE json_extract(j.value,'$.source_type') IS NOT NULL
 AND json_extract(j.value,'$.source_id') IS NOT NULL;

INSERT INTO memory_card_versions(card_id,revision,snapshot_json,changed_at)
SELECT memory_item_id,revision,snapshot_json,changed_at FROM memory_item_versions;

DROP TABLE memory_legacy_proposals;
DROP TABLE memory_item_versions;
DROP TABLE memory_items;
CREATE UNIQUE INDEX uq_memory_active_slot ON memory_cards(scope_key,slot_key)
 WHERE status='active' AND slot_key IS NOT NULL;
