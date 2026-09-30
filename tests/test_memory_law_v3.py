from __future__ import annotations

import sqlite3

import pytest

from memory_core import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    CueDrivenRetriever,
    MemoryProfile,
    MemoryStore,
    PacketRenderer,
    SchemaVersionError,
    ValidatedIntake,
    estimate_packet_tokens,
)


def evidence(label: str, **overrides) -> dict:
    item = {
        "evidence_type": "synthetic",
        "source_ref": f"test:{label}",
        "content_summary": label,
        "confidence": 0.9,
        "privacy_class": "synthetic",
        "source_payload": {"label": label},
    }
    item.update(overrides)
    return item


def create_claim(path, record_id: str = "claim"):
    store = MemoryStore(path)
    store.initialize()
    intake = ValidatedIntake(store, surface="test")
    intake.submit(
        operation_type="create",
        record_id=record_id,
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="bounded claim",
        logic="The bounded source supports the claim.",
        truth_basis="The test fixture is the source.",
        evidence=[evidence(record_id)],
        idempotency_key=f"create:{record_id}",
        changes={"title": "Claim", "summary": "A bounded current claim."},
    )
    return store, intake


def test_ordinary_reads_do_not_create_or_migrate_a_database(tmp_path):
    path = tmp_path / "absent" / "memory.sqlite3"
    store = MemoryStore(path)

    assert store.current_view() == []
    assert store.historical_view("missing") == []
    assert store.evidence_for_revision("missing@r1") == []
    assert not path.exists()
    assert not path.parent.exists()


def test_schema_identity_is_explicit_and_future_versions_fail_closed(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    initialized = store.initialize()

    assert initialized["application_id"] == APPLICATION_ID
    assert initialized["user_version"] == SCHEMA_VERSION
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaVersionError):
        store.current_view()


def test_semantics_and_lifecycle_are_physically_immutable(tmp_path):
    store, intake = create_claim(tmp_path / "memory.sqlite3")
    intake.submit(
        operation_type="refine",
        record_id="claim",
        actor="agent",
        reason="new bounded evidence",
        logic="The new statement contains the prior claim with more precision.",
        truth_basis="A second bounded source is attached.",
        evidence=[evidence("claim-r2")],
        idempotency_key="refine:claim",
        changes={"summary": "A more precise bounded current claim."},
    )

    history = store.historical_view("claim")
    assert [item["revision_status"] for item in history] == [
        "superseded",
        "current",
    ]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with store.connect() as conn:
            conn.execute(
                "UPDATE memory_revisions_v3 SET summary='rewritten' "
                "WHERE revision_id=?",
                (history[0]["revision_id"],),
            )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with store.connect() as conn:
            conn.execute(
                "UPDATE memory_lifecycle_events_v3 SET reason='rewritten'"
            )


def test_readonly_retrieval_is_separate_from_cognition_telemetry(tmp_path):
    store, _ = create_claim(tmp_path / "memory.sqlite3")
    store.add_cue(
        profile="test",
        cue="bounded current",
        target_record_id="claim",
        weight=2.0,
    )
    profile = MemoryProfile(name="test", packet_title="TEST")
    retriever = CueDrivenRetriever(store, profile)
    before = store.current_view("claim")[0]

    hits = retriever.retrieve(
        "bounded current",
        surface="test",
        track_access=False,
    )
    after_readonly = store.current_view("claim")[0]
    retriever.retrieve("bounded current", surface="test", track_access=True)
    after_tracked = store.current_view("claim")[0]

    assert hits[0].revision["record_id"] == "claim"
    assert after_readonly["access_count"] == before["access_count"]
    assert after_readonly["accessibility"] == before["accessibility"]
    assert after_tracked["access_count"] == before["access_count"] + 1
    assert after_tracked["accessibility"] > before["accessibility"]
    assert after_tracked["content_sha256"] == before["content_sha256"]


def test_evidence_identity_is_versioned_normalized_and_deterministic(tmp_path):
    store, intake = create_claim(tmp_path / "memory.sqlite3")
    intake.submit(
        operation_type="refine",
        record_id="claim",
        actor="agent",
        reason="same source family, independent observation",
        logic="The source adds a bounded detail.",
        truth_basis="The canonical evidence identity is explicit.",
        evidence=[
            evidence(
                "identity",
                source_family="  TEST FAMILY ",
                independence_group=" Case A ",
            )
        ],
        idempotency_key="refine:identity",
        changes={"summary": "Canonical evidence identities are explicit."},
    )
    current = store.current_view("claim")[0]
    linked = store.evidence_for_revision(current["revision_id"])

    assert linked[0]["identity_version"] == "evidence-v2"
    assert linked[0]["source_family"] == "test-family"
    assert linked[0]["independence_group"] == "case-a"
    assert linked[0]["evidence_id"].startswith("evidence:evidence-v2:")


def test_renderer_owns_the_complete_hard_packet_budget(tmp_path):
    store, _ = create_claim(tmp_path / "memory.sqlite3")
    profile = MemoryProfile(
        name="test",
        packet_title="TEST PACKET",
        bootstrap_record_ids=("claim",),
        default_instructions=(
            "Treat memory as candidate context.",
            "The current task and verified evidence decide truth.",
        ),
    )
    hits = CueDrivenRetriever(store, profile).retrieve(
        "claim",
        surface="test",
        track_access=False,
    )
    packet = PacketRenderer(profile).render(
        "claim",
        hits,
        scope="global",
        surface="test",
        token_budget=1200,
    )

    assert "Execution instruction" in packet
    assert "memory_id: `claim`" in packet
    assert estimate_packet_tokens(packet) <= 1200
    with pytest.raises(ValueError, match="framing and instructions"):
        PacketRenderer(profile).render(
            "claim",
            hits,
            scope="global",
            surface="test",
            token_budget=64,
        )


LEGACY_V2_SCHEMA = """
CREATE TABLE memory_records_v2 (
    record_id TEXT PRIMARY KEY, record_class TEXT NOT NULL, domain TEXT NOT NULL,
    scope TEXT NOT NULL, record_status TEXT NOT NULL, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE memory_revisions_v2 (
    revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL,
    parent_revision_id TEXT, revision_number INTEGER NOT NULL,
    title TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL,
    impact TEXT NOT NULL, confidence REAL NOT NULL, salience REAL NOT NULL,
    stability REAL NOT NULL, accessibility REAL NOT NULL, valid_from TEXT,
    valid_to TEXT, revision_status TEXT NOT NULL, authority_status TEXT NOT NULL,
    content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, surface TEXT NOT NULL, model_family TEXT NOT NULL,
    reason TEXT NOT NULL, idempotency_key TEXT
);
CREATE TABLE memory_evidence_v2 (
    evidence_id TEXT PRIMARY KEY, evidence_type TEXT NOT NULL,
    source_ref TEXT NOT NULL, source_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL, actor TEXT NOT NULL, surface TEXT NOT NULL,
    model_family TEXT NOT NULL, content_summary TEXT NOT NULL,
    confidence REAL NOT NULL, privacy_class TEXT NOT NULL
);
CREATE TABLE memory_revision_evidence_v2 (
    revision_id TEXT NOT NULL, evidence_id TEXT NOT NULL, stance TEXT NOT NULL,
    weight REAL NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE memory_meta_v2 (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def make_legacy_v2(path, *, orphan_revision: bool = False):
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_V2_SCHEMA)
        if not orphan_revision:
            conn.execute(
                "INSERT INTO memory_records_v2 VALUES(?,?,?,?,?,?,?)",
                (
                    "legacy",
                    "belief",
                    "semantic",
                    "global",
                    "active",
                    "2026-01-01T00:00:00+00:00",
                    "legacy-agent",
                ),
            )
        conn.execute(
            "INSERT INTO memory_revisions_v2 VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy@r1",
                "legacy",
                None,
                1,
                "Legacy",
                "Preserved from v2.",
                "",
                "",
                0.8,
                0.7,
                0.6,
                0.5,
                "2026-01-01T00:00:00+00:00",
                None,
                "current",
                "canonical_reference",
                "legacy-semantic-hash",
                "2026-01-01T00:00:00+00:00",
                "legacy-agent",
                "legacy",
                "",
                "legacy fixture",
                "legacy:create",
            ),
        )
        conn.execute(
            "INSERT INTO memory_evidence_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-evidence",
                "observation",
                "legacy:fixture",
                "legacy-source-hash",
                "2026-01-01T00:00:00+00:00",
                "legacy-agent",
                "legacy",
                "",
                "Legacy evidence.",
                0.8,
                "synthetic",
            ),
        )
        conn.execute(
            "INSERT INTO memory_revision_evidence_v2 VALUES(?,?,?,?,?)",
            ("legacy@r1", "legacy-evidence", "supports", 1.0, "legacy fixture"),
        )
        conn.execute(
            "INSERT INTO memory_meta_v2 VALUES('schema_version','2')"
        )


def test_copy_migration_preserves_source_and_is_idempotent(tmp_path):
    source_path = tmp_path / "legacy.sqlite3"
    target_path = tmp_path / "migrated.sqlite3"
    make_legacy_v2(source_path)

    migrated = MemoryStore(source_path).migrate_to(target_path)

    assert migrated.current_view("legacy")[0]["summary"] == "Preserved from v2."
    assert migrated.evidence_for_revision("legacy@r1")[0]["source_ref"] == (
        "legacy:fixture"
    )
    assert migrated.initialize()["changed"] is False
    with sqlite3.connect(source_path) as source:
        assert source.execute("PRAGMA application_id").fetchone()[0] == 0
        assert source.execute(
            "SELECT summary FROM memory_revisions_v2"
        ).fetchone()[0] == "Preserved from v2."


def test_failed_copy_migration_leaves_original_v2_readable(tmp_path):
    source_path = tmp_path / "broken-v2.sqlite3"
    target_path = tmp_path / "failed-copy.sqlite3"
    make_legacy_v2(source_path, orphan_revision=True)

    with pytest.raises(sqlite3.DatabaseError):
        MemoryStore(source_path).migrate_to(target_path)

    with sqlite3.connect(source_path) as source:
        assert source.execute(
            "SELECT summary FROM memory_revisions_v2"
        ).fetchone()[0] == "Preserved from v2."
        assert source.execute("PRAGMA application_id").fetchone()[0] == 0
