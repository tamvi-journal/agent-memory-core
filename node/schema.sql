PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS memory_records_v3 (
    record_id TEXT PRIMARY KEY,
    record_class TEXT NOT NULL
        CHECK(record_class IN ('event', 'belief', 'axis')),
    domain TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_revisions_v3 (
    revision_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    parent_revision_id TEXT,
    revision_number INTEGER NOT NULL CHECK(revision_number > 0),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.7
        CHECK(confidence BETWEEN 0.0 AND 1.0),
    valid_from TEXT,
    authority_status TEXT NOT NULL
        CHECK(authority_status IN (
            'non_authoritative', 'canonical_reference', 'protected'
        )),
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    model_family TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    FOREIGN KEY(record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(parent_revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT,
    UNIQUE(record_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_memory_core_v3_history
ON memory_revisions_v3(record_id, revision_number DESC);

CREATE TABLE IF NOT EXISTS memory_lifecycle_events_v3 (
    lifecycle_event_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK(sequence_number > 0),
    lifecycle_state TEXT NOT NULL
        CHECK(lifecycle_state IN (
            'current', 'superseded', 'deprecated', 'invalidated', 'archived'
        )),
    effective_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    operation_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT,
    UNIQUE(record_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_memory_core_v3_lifecycle_record
ON memory_lifecycle_events_v3(record_id, sequence_number DESC);

CREATE INDEX IF NOT EXISTS idx_memory_core_v3_lifecycle_revision
ON memory_lifecycle_events_v3(revision_id, sequence_number DESC);

CREATE TABLE IF NOT EXISTS memory_telemetry_v3 (
    revision_id TEXT PRIMARY KEY,
    salience REAL NOT NULL DEFAULT 0.5
        CHECK(salience BETWEEN 0.0 AND 1.0),
    stability REAL NOT NULL DEFAULT 0.5
        CHECK(stability BETWEEN 0.0 AND 1.0),
    accessibility REAL NOT NULL DEFAULT 0.5
        CHECK(accessibility BETWEEN 0.0 AND 1.0),
    access_count INTEGER NOT NULL DEFAULT 0 CHECK(access_count >= 0),
    last_accessed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_evidence_v3 (
    evidence_id TEXT PRIMARY KEY,
    identity_version TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_family TEXT NOT NULL,
    independence_group TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    model_family TEXT NOT NULL DEFAULT '',
    content_summary TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    privacy_class TEXT NOT NULL DEFAULT 'private',
    UNIQUE(identity_version, evidence_sha256)
);

CREATE TABLE IF NOT EXISTS memory_revision_evidence_v3 (
    revision_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    stance TEXT NOT NULL
        CHECK(stance IN (
            'supports', 'contradicts', 'qualifies', 'contextualizes'
        )),
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(revision_id, evidence_id, stance),
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(evidence_id)
        REFERENCES memory_evidence_v3(evidence_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_relations_v3 (
    relation_id TEXT PRIMARY KEY,
    from_record_id TEXT NOT NULL,
    to_record_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source_revision_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    FOREIGN KEY(from_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(to_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT,
    UNIQUE(from_record_id, to_record_id, relation_type)
);

CREATE TABLE IF NOT EXISTS memory_cues_v3 (
    cue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cue TEXT NOT NULL,
    cue_norm TEXT NOT NULL,
    cue_type TEXT NOT NULL DEFAULT 'phrase',
    target_record_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    scope TEXT NOT NULL DEFAULT 'global',
    profile TEXT NOT NULL,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    UNIQUE(profile, cue_norm, target_record_id)
);

CREATE TABLE IF NOT EXISTS memory_operations_v3 (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    target_record_id TEXT,
    target_revision_id TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(target_revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_access_v3 (
    access_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cue_sha256 TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    retrieval_reason TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL DEFAULT 0,
    surface TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_intake_v3 (
    intake_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    target_record_id TEXT,
    proposal_sha256 TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL
        CHECK(status IN (
            'received', 'held', 'materialized', 'rejected', 'no_op'
        )),
    decision_reason TEXT NOT NULL DEFAULT '',
    operation_id TEXT,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(operation_id)
        REFERENCES memory_operations_v3(operation_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_meta_v3 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS memory_revision_state_v3 AS
SELECT
    r.record_id,
    r.record_class,
    r.domain,
    r.scope,
    CASE
        WHEN l.lifecycle_state IN ('invalidated', 'archived')
        THEN l.lifecycle_state
        ELSE 'active'
    END AS record_status,
    v.revision_id,
    v.parent_revision_id,
    v.revision_number,
    v.title,
    v.summary,
    v.content,
    v.impact,
    v.confidence,
    t.salience,
    t.stability,
    t.accessibility,
    t.access_count,
    t.last_accessed_at,
    v.valid_from,
    CASE
        WHEN l.lifecycle_state='current' THEN NULL
        ELSE l.effective_at
    END AS valid_to,
    l.lifecycle_state AS revision_status,
    v.authority_status,
    v.content_sha256,
    v.created_at,
    v.created_by,
    v.surface,
    v.model_family,
    v.reason,
    v.idempotency_key
FROM memory_records_v3 r
JOIN memory_revisions_v3 v ON v.record_id=r.record_id
JOIN memory_telemetry_v3 t ON t.revision_id=v.revision_id
JOIN memory_lifecycle_events_v3 l
  ON l.lifecycle_event_id=(
      SELECT l2.lifecycle_event_id
      FROM memory_lifecycle_events_v3 l2
      WHERE l2.revision_id=v.revision_id
      ORDER BY l2.sequence_number DESC,l2.lifecycle_event_id DESC
      LIMIT 1
  );

CREATE VIEW IF NOT EXISTS memory_current_v3 AS
SELECT s.*
FROM memory_revision_state_v3 s
WHERE s.revision_status='current'
  AND NOT EXISTS (
      SELECT 1
      FROM memory_lifecycle_events_v3 newer
      WHERE newer.record_id=s.record_id
        AND newer.sequence_number > (
            SELECT MAX(current_event.sequence_number)
            FROM memory_lifecycle_events_v3 current_event
            WHERE current_event.revision_id=s.revision_id
        )
  );

CREATE TRIGGER IF NOT EXISTS memory_records_v3_no_update
BEFORE UPDATE ON memory_records_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_records_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_records_v3_no_delete
BEFORE DELETE ON memory_records_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_records_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_revisions_v3_no_update
BEFORE UPDATE ON memory_revisions_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_revisions_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_revisions_v3_no_delete
BEFORE DELETE ON memory_revisions_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_revisions_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_evidence_v3_no_update
BEFORE UPDATE ON memory_evidence_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_evidence_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_evidence_v3_no_delete
BEFORE DELETE ON memory_evidence_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_evidence_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_revision_evidence_v3_no_update
BEFORE UPDATE ON memory_revision_evidence_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_revision_evidence_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_revision_evidence_v3_no_delete
BEFORE DELETE ON memory_revision_evidence_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_revision_evidence_v3 is immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_lifecycle_events_v3_no_update
BEFORE UPDATE ON memory_lifecycle_events_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_lifecycle_events_v3 is append-only');
END;

CREATE TRIGGER IF NOT EXISTS memory_lifecycle_events_v3_no_delete
BEFORE DELETE ON memory_lifecycle_events_v3
BEGIN
    SELECT RAISE(ABORT, 'memory_lifecycle_events_v3 is append-only');
END;

CREATE TRIGGER IF NOT EXISTS memory_lifecycle_events_v3_owner_guard
BEFORE INSERT ON memory_lifecycle_events_v3
WHEN NOT EXISTS (
    SELECT 1
    FROM memory_revisions_v3 revision
    WHERE revision.revision_id=NEW.revision_id
      AND revision.record_id=NEW.record_id
)
BEGIN
    SELECT RAISE(ABORT, 'lifecycle revision does not belong to record');
END;

CREATE TRIGGER IF NOT EXISTS memory_lifecycle_events_v3_current_guard
BEFORE INSERT ON memory_lifecycle_events_v3
WHEN NEW.lifecycle_state='current'
  AND EXISTS (
      SELECT 1
      FROM memory_lifecycle_events_v3 previous
      WHERE previous.record_id=NEW.record_id
        AND previous.sequence_number=(
            SELECT MAX(latest.sequence_number)
            FROM memory_lifecycle_events_v3 latest
            WHERE latest.record_id=NEW.record_id
        )
        AND previous.lifecycle_state='current'
  )
BEGIN
    SELECT RAISE(ABORT, 'record already has a current revision');
END;

-- Schema v4: relations are an append-only event stream.
-- memory_relations_v3 is kept only as migration evidence and is no longer
-- written or read by the runtime.
CREATE TABLE IF NOT EXISTS memory_relation_events_v4 (
    relation_event_id TEXT PRIMARY KEY,
    relation_id TEXT NOT NULL,
    from_record_id TEXT NOT NULL,
    to_record_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 1),
    event_type TEXT NOT NULL CHECK(event_type IN ('assert', 'retract')),
    weight REAL NOT NULL DEFAULT 1.0,
    source_revision_id TEXT,
    evidence_id TEXT,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(from_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(to_record_id)
        REFERENCES memory_records_v3(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_revision_id)
        REFERENCES memory_revisions_v3(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(evidence_id)
        REFERENCES memory_evidence_v3(evidence_id) ON DELETE RESTRICT,
    UNIQUE(relation_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_memory_core_v4_relation_triple
ON memory_relation_events_v4(from_record_id, to_record_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_memory_core_v4_relation_sequence
ON memory_relation_events_v4(relation_id, sequence_number DESC);

CREATE TRIGGER IF NOT EXISTS memory_relation_events_v4_no_update
BEFORE UPDATE ON memory_relation_events_v4
BEGIN
    SELECT RAISE(ABORT, 'memory_relation_events_v4 is append-only');
END;

CREATE TRIGGER IF NOT EXISTS memory_relation_events_v4_no_delete
BEFORE DELETE ON memory_relation_events_v4
BEGIN
    SELECT RAISE(ABORT, 'memory_relation_events_v4 is append-only');
END;

CREATE VIEW IF NOT EXISTS memory_relation_current_v4 AS
SELECT
    e.relation_id,
    e.from_record_id,
    e.to_record_id,
    e.relation_type,
    e.weight,
    e.source_revision_id,
    e.evidence_id,
    'active' AS status,
    e.sequence_number,
    e.created_at
FROM memory_relation_events_v4 e
WHERE e.event_type='assert'
  AND e.sequence_number=(
      SELECT MAX(latest.sequence_number)
      FROM memory_relation_events_v4 latest
      WHERE latest.relation_id=e.relation_id
  );
