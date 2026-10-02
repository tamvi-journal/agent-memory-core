"""R2d oracle patches and frozen CLI invariants. All roots are temporary."""
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from memory_core import MemoryStore
from memory_core.observation import doctor, validate_store
from trajecta_identity import cli
from trajecta_identity.authority import ConfirmationMismatch, require_confirmation
from trajecta_identity.recipe import resolve

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "spec/golden/identity-open/store.sqlite3"
EXAMPLE = ROOT / "trajecta_identity/profiles/example"


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for key, folder in {"HOME": "home", "USERPROFILE": "home", "XDG_DATA_HOME": "xdg",
                        "LOCALAPPDATA": "local", "APPDATA": "app",
                        "TRAJECTA_IDENTITY_DATA_DIR": "data",
                        "TRAJECTA_IDENTITY_PROFILES": "profiles"}.items():
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.delenv("TRAJECTA_WORK_ROOT", raising=False)
    monkeypatch.delenv("TRAJECTA_IDENTITY_PROFILE", raising=False)
    shutil.copytree(EXAMPLE, tmp_path / "profiles/example")


def tree(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() if p.is_file() else None
            for p in path.rglob("*")}


@pytest.mark.parametrize("state", ["missing", "unknown", "legacy-v4", "ready"])
def test_doctor_never_initializes(state, tmp_path, monkeypatch, capsys):
    path = tmp_path / "store.sqlite3"
    if state == "unknown":
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=0")
    elif state == "legacy-v4":
        shutil.copyfile(LEGACY, path)
    elif state == "ready":
        MemoryStore(path).initialize()
    before = tree(tmp_path)
    monkeypatch.setattr(MemoryStore, "initialize", lambda *a, **k: pytest.fail("doctor initialized"))
    if state == "ready":
        cli.main(["--db", str(path), "doctor"])
    else:
        with pytest.raises(SystemExit) as error:
            cli.main(["--db", str(path), "doctor"])
        assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert list(result)[:3] == ["schema", "state", "passed"]
    assert result["state"] == state
    if state == "ready":
        assert list(result["checks"]) == ["integrity_ok", "foreign_keys_ok",
            "one_current_revision_per_record", "no_orphan_current_revision",
            "relations_carried_to_event_stream", "schema_application_id", "schema_version_current"]
    assert tree(tmp_path) == before


def test_validate_store_still_initializes(tmp_path):
    store = MemoryStore(tmp_path / "s.sqlite3")
    assert validate_store(store)["schema"] == "memory-core-doctor/v1"
    assert store.db_path.exists()


@pytest.mark.parametrize("argv", [["status"], ["retrieve", "who are you"], ["timeline"],
    ["core-proposals"], ["work"], ["--help"], ["retrieve"]])
def test_reads_usage_help_never_create_store(argv, tmp_path, capsys):
    path = tmp_path / "store.sqlite3"
    try:
        cli.main(["--db", str(path), *argv])
    except SystemExit:
        pass
    assert not path.exists()
    assert not list(tmp_path.glob("*.sqlite3*"))


def test_failed_resolve_preserves_last_choice(tmp_path):
    resolve("example")
    last = tmp_path / "data/.last-profile"
    before = last.read_bytes()
    with pytest.raises(FileNotFoundError):
        resolve("missing-profile")
    assert last.read_bytes() == before


@pytest.mark.parametrize("error", [ValueError("public"), RuntimeError("internal")])
def test_public_handler_does_not_hide_internal_crashes(error, monkeypatch, capsys):
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(cli, "resolve", fail)
    if isinstance(error, ValueError):
        with pytest.raises(SystemExit) as caught:
            cli.main(["status"])
        assert caught.value.code == 1
        assert capsys.readouterr().err == "ValueError: public\n"
    else:
        with pytest.raises(RuntimeError, match="internal"):
            cli.main(["status"])


def test_confirmation_invalid_utf8_is_typed():
    class Input:
        def isatty(self): return True
        def readline(self): return b"\xff".decode("utf8")
    class Output(io.StringIO):
        def isatty(self): return True
    with pytest.raises(ConfirmationMismatch, match="confirmation input was not valid UTF-8"):
        require_confirmation("APPLY abc", stdin=Input(), stdout=Output())


@pytest.mark.parametrize("alias", ["same", "symlink", "casefold"])
def test_migration_alias_keeps_backup_and_source(alias, tmp_path):
    source = tmp_path / "source.sqlite3"
    shutil.copyfile(LEGACY, source)
    original = source.read_bytes()
    target = tmp_path / "out/target.sqlite3"
    target.parent.mkdir()
    backup = target
    if alias == "symlink":
        if os.name == "nt": pytest.skip("symlinked-parent smoke is POSIX only")
        (tmp_path / "alias").symlink_to(target.parent, target_is_directory=True)
        backup = tmp_path / "alias/target.sqlite3"
    elif alias == "casefold":
        backup = target.with_name("TARGET.sqlite3")
        probe = tmp_path / "caseprobe"
        probe.write_bytes(b"probe")
        if not (tmp_path / "CASEPROBE").exists():
            pytest.skip("filesystem is case-sensitive")
    expected = ValueError if alias == "same" or (os.name == "nt" and alias == "casefold") else FileExistsError
    with pytest.raises(expected):
        MemoryStore(source).migrate_to(target, backup_path=backup)
    assert source.read_bytes() == original
    if expected is FileExistsError:
        assert backup.read_bytes() == original
        assert target.read_bytes() == original
    else:
        assert not target.exists()


def test_missing_source_ignores_equal_backup(tmp_path):
    result = MemoryStore(tmp_path / "missing").migrate_to(tmp_path / "target", backup_path=tmp_path / "target")
    assert result.schema_info()["state"] == "ready"


def test_equal_backup_refusal_precedes_directory_creation(tmp_path):
    source = tmp_path / "source.sqlite3"
    shutil.copyfile(LEGACY, source)
    target = tmp_path / "absent-parent/target.sqlite3"
    before = tree(tmp_path)
    with pytest.raises(ValueError, match="backup path must differ from target"):
        MemoryStore(source).migrate_to(target, backup_path=target)
    assert tree(tmp_path) == before
