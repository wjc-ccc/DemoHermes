-- 停用手册同步和 Task 三态；保留手册版本、原始事实与卡片审计。
UPDATE jobs SET status='cancelled', last_error='manual card sync retired',
 lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, revision=revision+1
WHERE job_type IN ('memory.manual_sync','memory.manual_maintain')
 AND status NOT IN ('completed','cancelled');

-- 旧提炼作业保留固定来源批次；已完成的结果不再执行。
UPDATE jobs SET handler_version='memory-extractor-v3', revision=revision+1
WHERE job_type='memory.extract' AND status<>'completed'
 AND handler_version<>'memory-extractor-v3';

-- 只有手册证据、没有对话或工具原文的旧卡进入隔离区。
-- 在历史表中写入新版本，留下状态变更原因。
INSERT INTO memory_card_versions(card_id,revision,snapshot_json,changed_at)
SELECT c.id,c.revision+1,
 json_set(v.snapshot_json,'$.status','quarantined','$.revision',c.revision+1,
          '$.metadata.quarantine_reason','manual_card_without_raw_evidence'),
 CAST(strftime('%s','now') AS INTEGER)*1000
FROM memory_cards c
JOIN memory_card_versions v ON v.card_id=c.id AND v.revision=c.revision
WHERE c.status='active'
 AND (c.source_type='manual_document' OR EXISTS(
   SELECT 1 FROM memory_evidence e WHERE e.card_id=c.id AND e.source_type='manual'))
 AND NOT EXISTS(
   SELECT 1 FROM memory_evidence e WHERE e.card_id=c.id
    AND e.source_type IN ('message','tool_call'));
UPDATE memory_cards
SET status='quarantined', revision=revision+1,
 metadata_json=json_set(metadata_json,'$.quarantine_reason','manual_card_without_raw_evidence'),
 updated_at=CAST(strftime('%s','now') AS INTEGER)*1000
WHERE status='active'
 AND (source_type='manual_document' OR EXISTS(
   SELECT 1 FROM memory_evidence e WHERE e.card_id=memory_cards.id AND e.source_type='manual'))
 AND NOT EXISTS(
   SELECT 1 FROM memory_evidence e WHERE e.card_id=memory_cards.id
    AND e.source_type IN ('message','tool_call'));

DROP TABLE IF EXISTS memory_document_sync;
DROP TABLE IF EXISTS goal_contract_versions;
DROP TABLE IF EXISTS workspace_items;
DROP TABLE IF EXISTS narrative_events;
DROP TABLE IF EXISTS session_state_items;
DROP TABLE IF EXISTS session_workspaces;
ALTER TABLE tool_calls DROP COLUMN workspace_item_id;
ALTER TABLE tool_calls DROP COLUMN provenance_json;

