from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "spec" / "golden-writes-v5"


def test_writes_manifest_authenticates_every_file_and_frozen_table():
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "trajecta.golden-writes-manifest/v1"
    assert manifest["oracle_semantics"] == [
        "R0-frozen", "R2a-authority-v2", "P4-decay-v4-refusal", "P5-writer-v4-precheck",
    ]
    expected = {
        path.relative_to(CORPUS).as_posix()
        for path in CORPUS.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    }
    expected |= {
        f"tables/{path.name}" for path in (ROOT / "memory_core" / "tables").glob("*.json")
    }
    assert set(manifest["files"]) == expected
    for relative, digest in manifest["files"].items():
        path = (ROOT / "memory_core" / relative) if relative.startswith("tables/") else (CORPUS / relative)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_writes_corpus_has_every_settled_scenario_group():
    assert {path.name for path in CORPUS.iterdir() if path.is_dir()} == {
        "bootstrap", "phase", "work-refs", "work-wrong-schema", "work-no-root",
        "fact-loop", "intake", "kernel", "recall", "decay", "decay-boundary",
        "decay-half", "legacy-v4", "migration-v2", "migration-v3", "migration-v4",
    }
