BEGIN TRANSACTION;
CREATE TABLE assets (
    content_hash TEXT NOT NULL,
    media_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_updated_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, media_name)
);
CREATE TABLE block_assets (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    media_name TEXT NOT NULL,
    PRIMARY KEY (block_id, content_hash, media_name),
    FOREIGN KEY (content_hash, media_name)
        REFERENCES assets(content_hash, media_name) ON DELETE CASCADE
);
CREATE TABLE block_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);
CREATE TABLE block_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_block_id, label)
);
CREATE TABLE block_user_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);
CREATE TABLE blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (document_id, parent_id, position)
);
INSERT INTO "blocks" VALUES('01M7PKKDN5GKX6H7FXXYWDRC8G','captured',NULL,0,'paragraph','{}','{"markdown": "x"}','x','t','t');
INSERT INTO "blocks" VALUES('01M7PKKDN63RCNBPH37Y8C3DG5','imported',NULL,0,'paragraph','{}','{"markdown": "x"}','x','t','t');
INSERT INTO "blocks" VALUES('01M7PKKDN63RCNBPH37Y8C3DG6','written',NULL,0,'paragraph','{}','{"markdown": "x"}','x','t','t');
PRAGMA writable_schema=ON;
CREATE TABLE change_log (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               seq INTEGER NOT NULL,
               epoch TEXT NOT NULL
           );
INSERT INTO "change_log" VALUES(1,11,'e57db8dd0e17237b');
CREATE TABLE document_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    patches_json TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0 CHECK (undone IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE document_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_document_id, label)
);
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('rough', 'polished')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    folder_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    title_auto INTEGER NOT NULL DEFAULT 0,
    change_seq INTEGER NOT NULL DEFAULT 0);
INSERT INTO "documents" VALUES('captured','C','rough','[]','01M7PKKDN45WWY36ECJPHKT338',0,NULL,'t','t',0,0,5);
INSERT INTO "documents" VALUES('imported','I','rough','[]',NULL,0,NULL,'t','t',0,0,8);
INSERT INTO "documents" VALUES('written','W','rough','[]',NULL,1,NULL,'t','t',0,0,11);
CREATE TABLE folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
INSERT INTO "folders" VALUES('01M7PKKDN45WWY36ECJPHKT338',NULL,'Uncategorized',0,'2026-11-18T05:55:44.932780+00:00','2026-11-18T05:55:44.932780+00:00');
CREATE TABLE link_check_cache (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_id TEXT,
    operation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE saved_searches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'text',
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO "schema_migrations" VALUES(12,'2026-11-18T05:55:44.931185+00:00');
CREATE TABLE transaction_receipts (
    transaction_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX folders_parent_position ON folders(parent_id, position);
CREATE INDEX blocks_document_position
    ON blocks(document_id, parent_id, position);
CREATE INDEX blocks_updated_at ON blocks(updated_at);
CREATE INDEX document_history_order
    ON document_history(document_id, id);
CREATE INDEX documents_folder_position ON documents(folder_id, position);
CREATE INDEX block_refs_target ON block_refs(target_block_id);
CREATE INDEX document_refs_target ON document_refs(target_document_id);
CREATE INDEX blocks_parent ON blocks(parent_id);
CREATE INDEX assets_media_name ON assets(media_name);
CREATE INDEX block_assets_asset ON block_assets(content_hash, media_name);
CREATE INDEX transaction_receipts_document ON transaction_receipts(document_id);
CREATE TRIGGER blocks_changed_insert AFTER INSERT ON blocks BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.document_id); END;
CREATE TRIGGER blocks_changed_update AFTER UPDATE ON blocks BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.document_id, OLD.document_id); END;
CREATE TRIGGER blocks_changed_delete AFTER DELETE ON blocks BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (OLD.document_id); END;
CREATE TRIGGER documents_changed_insert AFTER INSERT ON documents BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.id); END;
CREATE TRIGGER documents_changed_update AFTER UPDATE OF title, status, tags_json, folder_id, position, deleted_at, created_at, updated_at, revision, title_auto, provenance_json, source_kind, source_external_id, sync_json ON documents BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.id); END;
CREATE TRIGGER documents_changed_delete AFTER DELETE ON documents BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; END;
CREATE TRIGGER folders_changed_insert AFTER INSERT ON folders BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; END;
CREATE TRIGGER folders_changed_update AFTER UPDATE ON folders BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; END;
CREATE TRIGGER folders_changed_delete AFTER DELETE ON folders BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; END;
CREATE TRIGGER document_history_changed_insert AFTER INSERT ON document_history BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.document_id); END;
CREATE TRIGGER document_history_changed_update AFTER UPDATE ON document_history BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (NEW.document_id); END;
CREATE TRIGGER document_history_changed_delete AFTER DELETE ON document_history BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (OLD.document_id); END;
CREATE TRIGGER block_user_attrs_changed_insert AFTER INSERT ON block_user_attrs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.block_id); END;
CREATE TRIGGER block_user_attrs_changed_update AFTER UPDATE ON block_user_attrs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.block_id); END;
CREATE TRIGGER block_user_attrs_changed_delete AFTER DELETE ON block_user_attrs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = OLD.block_id); END;
CREATE TRIGGER block_refs_changed_insert AFTER INSERT ON block_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.source_block_id); END;
CREATE TRIGGER block_refs_changed_update AFTER UPDATE ON block_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.source_block_id); END;
CREATE TRIGGER block_refs_changed_delete AFTER DELETE ON block_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = OLD.source_block_id); END;
CREATE TRIGGER document_refs_changed_insert AFTER INSERT ON document_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.source_block_id); END;
CREATE TRIGGER document_refs_changed_update AFTER UPDATE ON document_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = NEW.source_block_id); END;
CREATE TRIGGER document_refs_changed_delete AFTER DELETE ON document_refs BEGIN UPDATE change_log SET seq = seq + 1 WHERE id = 1; UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1) WHERE id IN (SELECT document_id FROM blocks WHERE id = OLD.source_block_id); END;
PRAGMA writable_schema=OFF;
COMMIT;
