"""R2a Q1 / spec §2.1: every normal read surface works on a legacy-v4 store,
with default arguments, and leaves it byte-identical. v5 keeps tracking."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trajecta_identity import cli
from trajecta_identity.identity import IdentityMemory
from trajecta_identity.mcp_server import IdentityServer
from trajecta_identity.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
V4 = ROOT / "spec" / "golden" / "identity-open" / "store.sqlite3"


@pytest.fixture
def v4(tmp_path: Path) -> Path:
    path = tmp_path / "store.sqlite3"
    shutil.copyfile(V4, path)
    return path


def _unchanged(path: Path, before: bytes):
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["store.sqlite3"]


def test_default_retrieve_on_v4_is_read_only(v4: Path):
    before = v4.read_bytes()
    memory = IdentityMemory(load_profile("example"), v4, surface="test")
    assert memory.store.schema_info()["state"] == "legacy-v4"
    packet = memory.retrieve("who are you")  # default track=True
    assert packet["items"]
    assert packet["open_core_proposals"] == []
    _unchanged(v4, before)


def test_mcp_identity_retrieve_on_v4_is_read_only(v4: Path):
    before = v4.read_bytes()
    server = IdentityServer(IdentityMemory(load_profile("example"), v4, surface="mcp"))
    result = server.call_tool("identity_retrieve", {"cue": "who are you"})
    assert result["items"]
    assert server.call_tool("identity_status", {})["store"] == "legacy-v4"
    _unchanged(v4, before)


def test_cli_view_on_v4_does_not_bootstrap(v4: Path, monkeypatch):
    before = v4.read_bytes()
    served = []
    import trajecta_identity.view as view

    monkeypatch.setattr(view, "serve", lambda memory, **kw: served.append(view.snapshot(memory)))
    cli.main(["--profile", "example", "--db", str(v4), "view", "--no-browser"])
    assert served and served[0]
    _unchanged(v4, before)


def test_default_retrieve_on_v5_still_tracks(tmp_path: Path):
    memory = IdentityMemory(load_profile("example"), tmp_path / "v5.sqlite3", surface="test")
    memory.bootstrap()
    assert memory.store.schema_info()["state"] == "ready"
    memory.log_phase("phase:probe", title="Probe", summary="tracking probe")
    with memory.store.connect(readonly=True) as conn:
        before = conn.execute("SELECT COUNT(*) FROM memory_access_v3").fetchone()[0]
    memory.retrieve("tracking probe")
    with memory.store.connect(readonly=True) as conn:
        after = conn.execute("SELECT COUNT(*) FROM memory_access_v3").fetchone()[0]
    assert after > before
