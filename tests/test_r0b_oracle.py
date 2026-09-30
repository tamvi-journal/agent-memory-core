from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from memory_core import (
    CueDrivenRetriever,
    IncompatibleJournalMode,
    MemoryProfile,
    MemoryStore,
    SchemaVersionError,
)
from memory_core import text as frozen


def _legacy(value: str, *, identity: bool) -> str:
    translate = str.maketrans({
        "đ": "d", "ð": "d", "ł": "l", "ø": "o", "ħ": "h", "ı": "i",
        "ŧ": "t", "æ": "ae", "œ": "oe", "þ": "th",
    })
    folded = value.casefold()
    if not identity:
        folded = folded.translate(translate)
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9_]+", folded))


def test_frozen_tables_match_every_assigned_unicode14_code_point():
    if unicodedata.unidata_version != "14.0.0":
        pytest.skip(f"requires Unicode 14.0.0; running {unicodedata.unidata_version}")
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF or unicodedata.category(chr(cp)) == "Cn":
            continue
        char = chr(cp)
        for identity, normalizer in (
            (False, frozen.normalize_text), (True, frozen.normalize_identity_v1)
        ):
            assert normalizer(char) == _legacy(char, identity=identity), hex(cp)
            assert normalizer(f"a{char}b") == _legacy(f"a{char}b", identity=identity), hex(cp)


def test_frozen_table_digest_is_checked(tmp_path: Path, monkeypatch):
    file = tmp_path / "text-norm-v2.json"
    file.write_bytes((frozen._TABLES / file.name).read_bytes() + b" ")
    monkeypatch.setattr(frozen, "_TABLES", tmp_path)
    frozen._table.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            frozen.normalize_text("hello")
    finally:
        frozen._table.cache_clear()


def test_graph_frontier_reasons_are_record_id_sorted():
    def row(record_id):
        return dict(record_id=record_id, record_class="belief", domain="test",
                    authority_status="canonical_reference", scope="global", title="",
                    summary="", content="", confidence=0, salience=0,
                    stability=0, accessibility=1, revision_id=record_id + "@r1")

    class Store:
        def current_view(self):
            return [row(name) for name in ("a", "target", "z")]

        def cue_rows(self, profile, scope):
            return [dict(cue=name, target_record_id=name, weight=1.0)
                    for name in ("z", "a")]

        def active_relation_rows(self):
            return [dict(from_record_id=name, to_record_id="target",
                         relation_type="linked", weight=1.0)
                    for name in ("a", "z")]

    hits = CueDrivenRetriever(Store(), MemoryProfile("p", "P")).retrieve(
        "a z", track_access=False
    )
    target = next(hit for hit in hits if hit.revision["record_id"] == "target")
    assert target.reasons[:2] == [
        "graph:a-[linked]->target:d1", "graph:z-[linked]->target:d1"
    ]


def test_equal_weight_cues_use_cue_id(tmp_path: Path):
    store = MemoryStore(tmp_path / "cue.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO memory_records_v3(record_id,record_class,domain,scope,created_at,created_by) "
            "VALUES('target','belief','test','global','2026-01-01T00:00:00+00:00','test')"
        )
        conn.execute("CREATE INDEX reverse_equal_cues ON memory_cues_v3(profile,scope,weight DESC,cue_id DESC)")
        for cue in ("first", "second"):
            conn.execute(
                "INSERT INTO memory_cues_v3(cue,cue_norm,cue_type,target_record_id,weight,scope,profile) "
                "VALUES(?,?, 'phrase','target',1.0,'global','p')",
                (cue, cue),
            )
    rows = store.cue_rows("p", "global")
    assert [row["cue"] for row in rows] == ["first", "second"]


def test_readonly_rejects_wal_header_and_writable_converts(tmp_path: Path):
    path = tmp_path / "store.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    with pytest.raises(IncompatibleJournalMode, match="PRAGMA journal_mode=DELETE"):
        store.current_view()
    assert store.initialize()["changed"] is False
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert store.current_view() == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_readonly_rejects_sidecars(tmp_path: Path, suffix: str):
    path = tmp_path / "store.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    Path(f"{path}{suffix}").touch()
    with pytest.raises(IncompatibleJournalMode, match="WAL sidecar"):
        store.current_view()


def _wal_store(path: Path, *, application_id: int | None = None, user_version: int | None = None) -> None:
    """A store whose header says WAL, closed cleanly so no sidecars remain."""

    MemoryStore(path).initialize()
    conn = sqlite3.connect(path)
    try:
        if application_id is not None:
            conn.execute(f"PRAGMA application_id={application_id}")
        if user_version is not None:
            conn.execute(f"PRAGMA user_version={user_version}")
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        conn.close()


def _snapshot(path: Path) -> tuple[list[str], bytes]:
    return sorted(p.name for p in path.parent.iterdir()), path.read_bytes()


def test_readonly_wal_refused_before_sqlite_opens(tmp_path: Path):
    path = tmp_path / "store.sqlite3"
    _wal_store(path)
    before = _snapshot(path)
    assert before[0] == ["store.sqlite3"]
    with pytest.raises(IncompatibleJournalMode, match="journal_mode=wal"):
        MemoryStore(path).current_view()
    with pytest.raises(IncompatibleJournalMode):
        MemoryStore(path).schema_info()
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "header",
    [{"application_id": 1234}, {"user_version": 5}],
    ids=["foreign", "future"],
)
def test_writable_refuses_foreign_or_future_wal_untouched(tmp_path: Path, header):
    path = tmp_path / "store.sqlite3"
    _wal_store(path, **header)
    before = _snapshot(path)
    assert before[0] == ["store.sqlite3"]
    with pytest.raises(SchemaVersionError, match="newer or foreign"):
        MemoryStore(path).initialize()
    assert _snapshot(path) == before


def test_owned_wal_converts_to_delete_on_writable_open(tmp_path: Path):
    path = tmp_path / "store.sqlite3"
    _wal_store(path)
    store = MemoryStore(path)
    assert store.initialize()["changed"] is False
    assert path.read_bytes()[18:20] == b"\x01\x01"
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert store.current_view() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.sqlite3"]


def test_vendored_plugin_carries_the_frozen_tables():
    from trajecta_identity.plugin import plugin_files

    files = plugin_files("example", target="posix", env={"TRAJECTA_IDENTITY_DATA_DIR": "~/x"})
    for name in ("text-norm-v2.json", "identity-v1.json"):
        assert files[f"memory_core/tables/{name}"] == (frozen._TABLES / name).read_bytes()
