"""P15 is an insertion law; moving it ahead of replay/P5 is a regression."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from memory_core import MemoryStore, MigrationRequiredError

FIELDS = (
    "evidence_type", "source_ref", "source_family", "independence_group",
    "captured_at", "actor", "surface", "model_family", "content_summary",
    "privacy_class", "identity_version",
)
INVALID = [None, True, 1, 1.0, [], {}]
ROOT = Path(__file__).resolve().parents[1]


def create(store: MemoryStore, evidence: dict, key: str = "p15"):
    return store.create_current(
        record_id="p15", record_class="belief", domain="fact", title="P15",
        actor="synthetic", reason="metadata contract", evidence=evidence,
        idempotency_key=key,
    )


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", INVALID, ids=["null", "bool", "int", "float", "array", "object"])
def test_p15_refuses_each_invalid_kind_without_committing(tmp_path: Path, field, value):
    path = tmp_path / "store.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    before = path.read_bytes()
    with pytest.raises(ValueError, match=rf"^evidence\.{field} must be a string$"):
        create(store, {field: value})
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.sqlite3"]
    with store.connect(readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_operations_v3").fetchone()[0] == 0


def test_p15_preserves_g1_replay_before_new_invalid_metadata(tmp_path: Path):
    path = tmp_path / "store.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    first = create(store, {"source_ref": "synthetic:original"})
    before = path.read_bytes()
    replay = create(store, {field: None for field in FIELDS})
    assert replay == first
    assert path.read_bytes() == before


def test_p15_does_not_move_before_p5(tmp_path: Path):
    path = tmp_path / "store.sqlite3"
    shutil.copyfile(ROOT / "spec/golden/identity-open/store.sqlite3", path)
    before = path.read_bytes()
    with pytest.raises(MigrationRequiredError, match="schema v4 store must be migrated to v5 before writing"):
        create(MemoryStore(path), {"source_ref": None})
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.sqlite3"]


def test_p15_accepts_absent_and_empty_strings_and_json_source_payload(tmp_path: Path):
    store = MemoryStore(tmp_path / "store.sqlite3")
    store.initialize()
    result = create(store, {
        **{field: "" for field in FIELDS},
        "source_payload": {"2": 1, "1": [None, True, 1.0]}, "confidence": 0.9,
    })
    assert result["revision"]["record_id"] == "p15"
