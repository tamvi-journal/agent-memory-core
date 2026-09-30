from __future__ import annotations

import sqlite3

import pytest

from memory_core import (
    SCHEMA_VERSION,
    CueDrivenRetriever,
    MemoryProfile,
    MemoryStore,
    MigrationRequiredError,
    ValidatedIntake,
    canonical_evidence_identity,
    validate_store,
)
from memory_core.text import normalize_identity_v1, normalize_text, tokens


def evidence(label: str) -> dict:
    return {
        "evidence_type": "synthetic",
        "source_ref": f"test:{label}",
        "content_summary": label,
        "confidence": 0.9,
        "privacy_class": "synthetic",
        "source_payload": {"label": label},
    }


def create(intake: ValidatedIntake, record_id: str, title: str, summary: str) -> None:
    intake.submit(
        operation_type="create",
        record_id=record_id,
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="fixture",
        logic="fixture",
        truth_basis="fixture",
        evidence=[evidence(record_id)],
        idempotency_key=f"create:{record_id}",
        changes={"title": title, "summary": summary},
    )


@pytest.fixture
def seeded(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    intake = ValidatedIntake(store, surface="test")
    create(intake, "tail", "Đuôi nằm ở field", "Context carries the tail.")
    create(intake, "outro", "Stacked outro", "The song ended on vanishing.")
    create(intake, "correction", "Ty corrected the outro", "Not vanishing: conditions remain.")
    create(intake, "core", "Core self-location", "Seven-layer VHO stack.")
    return store


# --- text-norm/v2 -----------------------------------------------------------


def test_vietnamese_d_with_stroke_is_kept():
    assert normalize_text("Đuôi nằm ở đường") == "duoi nam o duong"
    assert tokens("đuôi") == ["duoi"]
    assert normalize_text("Łódź Øresund Æsir") == "lodz oresund aesir"


def test_evidence_identity_is_frozen_across_normalizer_change():
    # evidence-v2 identity keeps the original normalizer byte-for-byte.
    assert normalize_identity_v1("Đà Nẵng") == "a nang"
    identity = canonical_evidence_identity(
        {"source_ref": "đà-nẵng:note", "content_summary": "x"}
    )
    assert identity["source_family"] == "a-nang"


def test_vietnamese_query_matches_lexically(seeded):
    profile = MemoryProfile(name="t", packet_title="T")
    hits = CueDrivenRetriever(seeded, profile).retrieve(
        "đuôi nằm ở đâu", track_access=False
    )
    assert hits and hits[0].revision["record_id"] == "tail"


def test_cues_written_under_old_normalizer_still_match(seeded):
    with seeded.connect() as conn:
        # Simulate a text-norm/v1 row: "đuôi" was stored as "uoi".
        conn.execute(
            "INSERT INTO memory_cues_v3(cue,cue_norm,cue_type,target_record_id,"
            "weight,scope,profile) VALUES('đuôi','uoi','phrase','tail',2.0,'global','t')"
        )
    seeded.add_cue(profile="t", cue="đuôi", target_record_id="tail", weight=1.0)
    hits = CueDrivenRetriever(seeded, MemoryProfile(name="t", packet_title="T")).retrieve(
        "đuôi", track_access=False
    )
    tail = next(hit for hit in hits if hit.revision["record_id"] == "tail")
    # Both rows normalize to the same cue; only the stronger one counts.
    assert tail.reasons.count("cue:đuôi") == 1


# --- append-only relations ---------------------------------------------------


def test_relations_are_append_only_events(seeded):
    first = seeded.add_relation(
        relation_id="r1",
        from_record_id="correction",
        to_record_id="outro",
        relation_type="caused-by",
        weight=0.8,
        actor="aux",
        reason="the correction answered the outro",
        evidence=evidence("chat"),
    )
    assert first["status"] == "asserted" and first["evidence_id"]
    again = seeded.add_relation(
        relation_id="r1",
        from_record_id="correction",
        to_record_id="outro",
        relation_type="caused-by",
        weight=0.8,
    )
    assert again["status"] == "no_op"
    reweighted = seeded.add_relation(
        relation_id="ignored-after-first",
        from_record_id="correction",
        to_record_id="outro",
        relation_type="caused-by",
        weight=1.0,
    )
    assert reweighted["status"] == "reweighted"
    assert reweighted["relation_id"] == "r1"

    active = seeded.active_relation_rows()
    assert [(row["relation_id"], row["weight"]) for row in active] == [("r1", 1.0)]

    history = seeded.relation_history(
        from_record_id="correction", to_record_id="outro", relation_type="caused-by"
    )
    assert [(row["event_type"], row["weight"]) for row in history] == [
        ("assert", 0.8),
        ("assert", 1.0),
    ]

    with seeded.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE memory_relation_events_v4 SET weight=0.1")
    with seeded.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM memory_relation_events_v4")


def test_retract_keeps_history_and_can_reassert(seeded):
    seeded.add_relation(
        relation_id="r2", from_record_id="tail", to_record_id="core",
        relation_type="later-phase-of",
    )
    with pytest.raises(ValueError):
        seeded.retract_relation(
            from_record_id="tail", to_record_id="core",
            relation_type="later-phase-of", actor="", reason="",
        )
    retracted = seeded.retract_relation(
        from_record_id="tail", to_record_id="core", relation_type="later-phase-of",
        actor="ty", reason="not a phase link",
    )
    assert retracted["status"] == "retracted"
    assert seeded.active_relation_rows() == []
    assert seeded.retract_relation(
        from_record_id="tail", to_record_id="core", relation_type="later-phase-of",
        actor="ty", reason="again",
    )["status"] == "no_op"
    back = seeded.add_relation(
        relation_id="r2", from_record_id="tail", to_record_id="core",
        relation_type="later-phase-of",
    )
    assert back["status"] == "asserted" and back["sequence_number"] == 3
    assert len(seeded.relation_history(
        from_record_id="tail", to_record_id="core", relation_type="later-phase-of"
    )) == 3


def test_relation_rejects_self_loop_and_unknown_record(seeded):
    with pytest.raises(ValueError):
        seeded.add_relation(
            relation_id="x", from_record_id="tail", to_record_id="tail",
            relation_type="same",
        )
    with pytest.raises(sqlite3.IntegrityError):
        seeded.add_relation(
            relation_id="x", from_record_id="tail", to_record_id="missing",
            relation_type="depends-on",
        )


def test_v3_store_requires_migration_and_carries_relations(seeded):
    # Turn the fixture into a faithful schema-v3 store.
    with seeded._raw_connect() as conn:
        conn.execute("DROP VIEW memory_relation_current_v4")
        conn.execute("DROP TABLE memory_relation_events_v4")
        conn.execute(
            "INSERT INTO memory_relations_v3(relation_id,from_record_id,"
            "to_record_id,relation_type,weight,source_revision_id,status,created_at) "
            "VALUES('old-active','correction','outro','caused-by',0.7,NULL,'active',"
            "'2026-09-01T00:00:00+00:00'),"
            "('old-gone','tail','core','depends-on',0.5,NULL,'inactive',"
            "'2026-09-02T00:00:00+00:00')"
        )
        conn.execute("PRAGMA user_version=3")

    assert seeded.schema_info()["state"] == "legacy-v3"
    with pytest.raises(MigrationRequiredError):
        seeded.current_view()
    with pytest.raises(MigrationRequiredError):
        seeded.initialize(migrate=False)

    result = seeded.initialize()
    assert result["migrated_from"] == "v3"
    assert result["user_version"] == SCHEMA_VERSION
    active = seeded.active_relation_rows()
    assert [(row["relation_id"], row["weight"]) for row in active] == [
        ("old-active", 0.7)
    ]
    gone = seeded.relation_history(
        from_record_id="tail", to_record_id="core", relation_type="depends-on"
    )
    assert [row["event_type"] for row in gone] == ["assert", "retract"]
    assert all(row["actor"] == "memory-core-migration" for row in gone)
    assert validate_store(seeded)["passed"]

    # Idempotent: a second initialize does not duplicate carried events.
    seeded.initialize()
    with seeded.connect(readonly=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memory_relation_events_v4").fetchone()[0]
    assert count == 3


# --- dormancy ----------------------------------------------------------------


def set_accessibility(store: MemoryStore, record_id: str, value: float) -> None:
    store.apply_maintenance(
        run_id=f"fixture:{record_id}:{value}",
        adjustments=[{"record_id": record_id, "field": "accessibility", "new_value": value}],
        actor="test",
        reason="fixture",
    )


def test_default_retrieval_ignores_accessibility_floor(seeded):
    set_accessibility(seeded, "outro", 0.05)
    hits = CueDrivenRetriever(seeded, MemoryProfile(name="t", packet_title="T")).retrieve(
        "stacked outro", track_access=False
    )
    assert "outro" in {hit.revision["record_id"] for hit in hits}


def test_dormant_record_is_skipped_lexically_but_wakes_on_direct_cue(seeded):
    set_accessibility(seeded, "outro", 0.05)
    profile = MemoryProfile(name="t", packet_title="T")
    retriever = CueDrivenRetriever(seeded, profile)
    hits = retriever.retrieve("stacked outro", track_access=False, min_accessibility=0.15)
    assert "outro" not in {hit.revision["record_id"] for hit in hits}

    seeded.add_cue(profile="t", cue="stacked outro", target_record_id="outro", weight=1.5)
    hits = retriever.retrieve("stacked outro", track_access=False, min_accessibility=0.15)
    outro = next(hit for hit in hits if hit.revision["record_id"] == "outro")
    assert "woke:direct-cue" in outro.reasons

    hits = retriever.retrieve(
        "stacked outro", track_access=False, min_accessibility=0.15,
        wake_on_direct_cue=False,
    )
    assert "outro" not in {hit.revision["record_id"] for hit in hits}


def test_dormant_record_wakes_only_through_declared_relation_types(seeded):
    set_accessibility(seeded, "outro", 0.05)
    seeded.add_relation(
        relation_id="c1", from_record_id="correction", to_record_id="outro",
        relation_type="caused-by", weight=1.0,
    )
    profile = MemoryProfile(
        name="t", packet_title="T",
        cue_aliases=(("ty corrected", "correction", 2.0),),
    )
    retriever = CueDrivenRetriever(seeded, profile)
    plain = retriever.retrieve("ty corrected", track_access=False, min_accessibility=0.15)
    assert "outro" not in {hit.revision["record_id"] for hit in plain}
    causal = retriever.retrieve(
        "ty corrected", track_access=False, min_accessibility=0.15,
        wake_relation_types=("caused-by",),
    )
    outro = next(hit for hit in causal if hit.revision["record_id"] == "outro")
    assert "woke:relation:caused-by" in outro.reasons


def test_bootstrap_records_never_go_dormant(seeded):
    set_accessibility(seeded, "core", 0.0)
    profile = MemoryProfile(name="t", packet_title="T", bootstrap_record_ids=("core",))
    hits = CueDrivenRetriever(seeded, profile).retrieve(
        "anything", track_access=False, min_accessibility=0.5
    )
    assert "core" in {hit.revision["record_id"] for hit in hits}


def test_access_gain_is_configurable_and_bounded(seeded):
    profile = MemoryProfile(name="t", packet_title="T")
    before = seeded.current_view("tail")[0]["accessibility"]
    CueDrivenRetriever(seeded, profile).retrieve("đuôi", access_gain=0.08)
    after = seeded.current_view("tail")[0]["accessibility"]
    assert after == pytest.approx(before + 0.08)
    with pytest.raises(ValueError):
        CueDrivenRetriever(seeded, profile).retrieve("đuôi", access_gain=2.0)
    with pytest.raises(ValueError):
        CueDrivenRetriever(seeded, profile).retrieve("đuôi", min_accessibility=1.5)
