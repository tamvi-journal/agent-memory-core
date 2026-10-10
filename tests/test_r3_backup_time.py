"""Settled §3.6, through public migrations on fresh synthetic legacy copies."""
import os
from pathlib import Path
import shutil

import pytest

from tools.r3.common import isolated_env, snapshot
from tools.r3.run import run_case

ROOT = Path(__file__).resolve().parents[1]
UPPER = (1 << 32) * 10**9
MODERN = 1700000000123456000


def migration(tmp_path, runtime, mtime, *, gate=None, fault=None):
    isolated_env(tmp_path)
    source = tmp_path / "store.sqlite3"
    shutil.copyfile(ROOT / "spec/golden/identity-open/store.sqlite3", source)
    try:
        os.utime(source, ns=(mtime, mtime))
    except (OSError, OverflowError):
        pytest.skip("host cannot create this source timestamp")
    actual = source.stat().st_mtime_ns
    if actual // 1000 != mtime // 1000:
        pytest.skip("host cannot represent this source microsecond")
    target, backup = tmp_path / "target.sqlite3", tmp_path / "backup.sqlite3"
    if gate == "target": target.write_bytes(b"existing target")
    if gate == "equal": backup = target
    if gate == "backup": backup.write_bytes(b"existing backup")
    before = snapshot(tmp_path)
    output = run_case(runtime, tmp_path, {"surface": "cli", "backup_fault": fault, "argv": [
        "--profile", "example", "--db", str(source), "migrate-to", str(target), "--backup", str(backup),
    ]})
    after = snapshot(tmp_path)
    assert after["store.sqlite3"] == before["store.sqlite3"]
    for suffix in ("-wal", "-shm", "-journal"):
        assert ("store.sqlite3" + suffix in after) == ("store.sqlite3" + suffix in before)
    return output, source, target, backup


@pytest.mark.parametrize("runtime", ["py", "ts"])
@pytest.mark.parametrize("mtime", [0, UPPER - 1, *(MODERN + x for x in [0, 1, 499, 500, 999, 1999])])
def test_backup_supported_precision_edges(tmp_path, runtime, mtime):
    out, source, target, backup = migration(tmp_path, runtime, mtime)
    assert out["exit"] == 0 and not out["stderr"], out
    assert target.exists()
    assert backup.read_bytes() == source.read_bytes()
    assert backup.stat().st_mtime_ns // 1000 == source.stat().st_mtime_ns // 1000


@pytest.mark.parametrize("runtime", ["py", "ts"])
@pytest.mark.parametrize("mtime", [-1, UPPER])
def test_backup_unsupported_edges(tmp_path, runtime, mtime):
    out, _, target, backup = migration(tmp_path, runtime, mtime)
    assert out["exit"] == 1
    assert out["stderr"] == b"ValueError: source mtime is outside the supported backup range\n"
    assert not target.exists() and not backup.exists()


@pytest.mark.parametrize("runtime", ["py", "ts"])
@pytest.mark.parametrize("gate", ["target", "equal", "backup", None])
def test_backup_order_composition(tmp_path, runtime, gate):
    out, _, target, backup = migration(tmp_path, runtime, UPPER, gate=gate)
    assert out["exit"] == 1
    expected = {
        "target": f"FileExistsError: {target}\n",
        "equal": "ValueError: backup path must differ from target\n",
        "backup": f"FileExistsError: {backup}\n",
        None: "ValueError: source mtime is outside the supported backup range\n",
    }[gate]
    assert out["stderr"] == expected.encode()
    assert target.exists() == (gate == "target")
    assert backup.exists() == (gate == "backup" or gate == "target" and backup == target)


@pytest.mark.parametrize("runtime", ["py", "ts"])
@pytest.mark.parametrize("fault", ["set", "readback", "readback-missing"])
def test_backup_verification_failure_cleans_only_created_backup(tmp_path, runtime, fault):
    out, _, target, backup = migration(tmp_path, runtime, MODERN + 499, fault=fault)
    assert out["exit"] == 1
    assert out["stderr"] == b"ValueError: backup mtime could not be preserved to a microsecond\n"
    assert not target.exists() and not backup.exists()
