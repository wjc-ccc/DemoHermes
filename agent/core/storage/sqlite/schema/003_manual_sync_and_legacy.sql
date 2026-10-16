CREATE TABLE memory_document_sync (
 scope_key TEXT PRIMARY KEY,
 document_revision INTEGER NOT NULL,
 synced_revision INTEGER NOT NULL DEFAULT 0,
 updated_at INTEGER NOT NULL
);

-- Only cards proposed before this upgrade are candidates for revalidation.
CREATE TABLE memory_legacy_proposals (
 item_id TEXT PRIMARY KEY REFERENCES memory_items(id),
 result TEXT NOT NULL DEFAULT 'pending',
 detail TEXT
);
INSERT INTO memory_legacy_proposals(item_id)
SELECT id FROM memory_items WHERE status='proposed';
