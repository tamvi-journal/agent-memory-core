from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.golden_mcp.generate import ORACLE_COMMIT, SCENARIOS, UNCHANGED_AFTER_CALL

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "spec" / "golden-mcp-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mcp_manifest_authenticates_every_corpus_file_and_table():
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "trajecta.golden-mcp-manifest/v1"
    assert manifest["oracle_commit"] == ORACLE_COMMIT

    expected = {
        path.relative_to(CORPUS).as_posix()
        for path in CORPUS.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    }
    expected.update(f"tables/{path.name}" for path in (ROOT / "memory_core" / "tables").glob("*.json"))
    assert set(manifest["files"]) == expected
    for relative, digest in manifest["files"].items():
        path = ROOT / "memory_core" / relative if relative.startswith("tables/") else CORPUS / relative
        assert sha256(path) == digest, relative

    for relative, digest in manifest["oracle_sources"].items():
        assert sha256(ROOT / relative) == digest, relative


def test_mcp_corpus_has_every_declared_scenario_and_closed_shape():
    directories = {path.name for path in CORPUS.iterdir() if path.is_dir()}
    assert directories == set(SCENARIOS)
    for name in directories:
        files = {path.name for path in (CORPUS / name).iterdir() if path.is_file()}
        assert {"transcript.in", "expected.out"} <= files
        assert ("absent" in files) != ("store.sqlite3" in files)
        if "store.sqlite3" in files:
            assert "dump.json" in files


def test_refusal_scenarios_preserve_store_and_sidecar_bytes():
    for name in UNCHANGED_AFTER_CALL:
        scenario = CORPUS / name
        assert (scenario / "initial.sqlite3").read_bytes() == (scenario / "store.sqlite3").read_bytes(), name
        initial_sidecar = scenario / "initial.sqlite3-wal"
        final_sidecar = scenario / "store.sqlite3-wal"
        assert initial_sidecar.exists() == final_sidecar.exists(), name
        if initial_sidecar.exists():
            assert initial_sidecar.read_bytes() == final_sidecar.read_bytes(), name


def test_never_bootstrap_and_invalid_calls_have_no_store_artifact():
    for name in ("basic-methods", "call-structure", "framing", "ids", "initialize", "invalid-mutation", "missing-reads", "validation"):
        scenario = CORPUS / name
        assert (scenario / "absent").exists(), name
        assert not (scenario / "store.sqlite3").exists(), name
