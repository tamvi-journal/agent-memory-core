import pytest

from tools.r3.idempotency import intake


@pytest.mark.parametrize("variant", ["same", "different", "received"])
def test_p18_bidirectional_public_intake(tmp_path, variant):
    first, _ = intake(tmp_path / "py-loser", "py", "ts", variant)
    second, _ = intake(tmp_path / "ts-loser", "ts", "py", variant)
    assert first == second


def test_initialization_state_guard_is_inside_immediate(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from memory_core import MemoryStore
    store = MemoryStore(tmp_path / "store.sqlite3")
    store.initialize()
    original = MemoryStore._raw_connect
    traces = []
    @contextmanager
    def traced(self, **kwargs):
        with original(self, **kwargs) as conn:
            statements = []
            traces.append(statements)
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(MemoryStore, "_raw_connect", traced)
    assert store.initialize()["changed"] is False
    reads = [trace for trace in traces if any("sqlite_master" in sql for sql in trace)]
    assert reads
    for trace in reads:
        first = next(i for i, sql in enumerate(trace) if "sqlite_master" in sql)
        assert "BEGIN IMMEDIATE" in trace[:first], trace


def test_ready_delete_initialize_preserves_bytes_mtime_and_sidecars(tmp_path):
    from memory_core import MemoryStore
    path = tmp_path / "store.sqlite3"
    store = MemoryStore(path)
    store.initialize()
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in tmp_path.iterdir()))
    assert store.initialize()["changed"] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in tmp_path.iterdir())) == before


from tools.r3.idempotency import PUBLIC_SITES, excluded


@pytest.mark.parametrize("holder,victim", [("py", "ts"), ("ts", "py")])
@pytest.mark.parametrize("site", PUBLIC_SITES)
def test_excluded_public_same_key_gate_equals_serial(tmp_path, holder, victim, site):
    excluded(tmp_path, holder, victim, site)


@pytest.mark.parametrize("unit", ["_capture_evidence", "create_current", "_link_evidence", "_decide_intake"])
def test_p18_does_not_catch_constraints_from_other_units(tmp_path, monkeypatch, unit):
    import sqlite3
    from memory_core import MemoryStore, ValidatedIntake
    from tools.r3.crashes import intake_case
    store = MemoryStore(tmp_path / "store.sqlite3")
    store.initialize()
    intake_api = ValidatedIntake(store, surface="golden")
    original = sqlite3.IntegrityError("synthetic constraint")
    def fail(*args, **kwargs):
        raise original
    target = store if unit == "create_current" else intake_api
    monkeypatch.setattr(target, unit, fail)
    lookups = []
    lookup = intake_api._intake_by_key
    def observed(key):
        lookups.append(key)
        return lookup(key)
    monkeypatch.setattr(intake_api, "_intake_by_key", observed)
    with pytest.raises(sqlite3.IntegrityError) as caught:
        intake_api.submit(**intake_case()["arguments"]["proposal"])
    assert caught.value is original
    assert len(lookups) == 1


@pytest.mark.parametrize("condition", ["no-row", "wrong-id", "not-constraint", "lookup-fails"])
def test_p18_requires_the_complete_recovery_proof(tmp_path, monkeypatch, condition):
    import sqlite3
    from memory_core import MemoryStore, ValidatedIntake
    from tools.r3.crashes import intake_case
    store = MemoryStore(tmp_path / "store.sqlite3")
    store.initialize()
    api = ValidatedIntake(store, surface="golden")
    proposal = intake_case(unresolved_conflict=True)["arguments"]["proposal"]
    if condition == "lookup-fails":
        count = 0
        def failed_lookup(key):
            nonlocal count
            count += 1
            if count > 1: raise LookupError("fresh lookup cannot prove recovery")
            return None
        monkeypatch.setattr(api, "_intake_by_key", failed_lookup)
    if condition == "wrong-id":
        api.submit(**proposal)
        with store.connect() as conn:
            conn.execute("UPDATE memory_intake_v3 SET intake_id='intake:wrong'")
        lookup = api._intake_by_key
        count = 0
        def skip_fast_path(key):
            nonlocal count
            count += 1
            return None if count == 1 else lookup(key)
        monkeypatch.setattr(api, "_intake_by_key", skip_fast_path)
    original_insert = api._insert_intake
    raised = []
    def failed_insert(**args):
        try:
            if condition == "wrong-id":
                return original_insert(**args)
            with store.connect() as conn:
                conn.execute("INSERT INTO memory_meta_v3 VALUES('p18-rollback-proof','temporary')")
                if condition == "not-constraint":
                    raise sqlite3.OperationalError("synthetic non-constraint")
                conn.execute("INSERT INTO memory_intake_v3(intake_id) VALUES('incomplete')")
        except sqlite3.Error as error:
            raised.append(error)
            raise
    monkeypatch.setattr(api, "_insert_intake", failed_insert)
    with pytest.raises(sqlite3.Error) as caught:
        api.submit(**proposal)
    assert caught.value is raised[0]
    with store.connect(readonly=True) as conn:
        assert conn.execute("SELECT value FROM memory_meta_v3 WHERE key='p18-rollback-proof'").fetchone() is None
