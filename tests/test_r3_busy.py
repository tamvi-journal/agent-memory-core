"""P16 maps SQLite result codes, never approximate error strings or timing."""
import sqlite3

import pytest

from memory_core.store import StoreBusy, _translate_sqlite_error


@pytest.mark.parametrize("code", [5, 6, 261, 262, 517, 518, 773])
def test_busy_primary_and_extended_codes(code):
    error = sqlite3.OperationalError("private SQLite detail")
    error.sqlite_errorcode = code
    translated = _translate_sqlite_error(error)
    assert isinstance(translated, StoreBusy)
    assert str(translated) == "store is busy; retry later"


@pytest.mark.parametrize("message", ["database is locked", "database table is locked", "database schema is locked"])
def test_python310_exact_fallback(message):
    assert isinstance(_translate_sqlite_error(sqlite3.OperationalError(message)), StoreBusy)


@pytest.mark.parametrize("message", ["database is locked!", "database is locked ", "DATABASE IS LOCKED", "database table is locked: secret"])
def test_no_approximate_fallback(message):
    error = sqlite3.OperationalError(message)
    assert _translate_sqlite_error(error) is error


def test_available_nonbusy_code_wins_over_message():
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = 1
    assert _translate_sqlite_error(error) is error
    other = sqlite3.IntegrityError("database is locked")
    assert _translate_sqlite_error(other) is other
    error.sqlite_errorcode = None
    assert _translate_sqlite_error(error) is error


@pytest.mark.parametrize("holder,victim", [("py", "ts"), ("ts", "py")])
@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_real_bidirectional_busy_surface_and_rollback(tmp_path, holder, victim, surface):
    import shutil
    import time
    from pathlib import Path
    from tools.r3.busy import held_writer
    from tools.r3.common import isolated_env
    from tools.r3.plans import step
    from tools.r3.run import run_case, case_for_step
    root = Path(__file__).resolve().parents[1]
    env = isolated_env(tmp_path)
    database = tmp_path / "store.sqlite3"
    shutil.copyfile(root / "spec/golden-writes-v5/bootstrap/store.sqlite3", database)
    before = database.read_bytes()
    case = case_for_step(tmp_path, step(victim, surface, "log-phase",
                         event_id="blocked", title="Blocked", summary="synthetic"), 0, {})
    with held_writer(holder, database, env) as process:
        start = time.monotonic()
        result = run_case(victim, tmp_path, case)
        elapsed = time.monotonic() - start
        assert process.poll() is None
        assert elapsed >= 4.5  # Canonical SQLITE_BUSY only; no general timing contract.
        assert database.read_bytes() == before
        if surface == "cli":
            assert result == {"exit": 1, "stdout": b"", "stderr": b"StoreBusy: store is busy; retry later\n"}
        else:
            import json
            assert result["exit"] == 0 and not result["stderr"]
            wire = json.loads(result["stdout"])
            assert wire["result"] == {"content": [{"type": "text", "text": "StoreBusy: store is busy; retry later"}], "isError": True}
    from memory_core import MemoryStore
    store = MemoryStore(database)
    assert store.schema_info()["state"] == "ready"
    with store.connect(readonly=True) as connection:
        assert connection.execute("SELECT count(*) FROM memory_records_v3 WHERE record_id='phase:blocked'").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_operations_v3 WHERE idempotency_key='p16-holder'").fetchone()[0] == 1
    assert not any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
