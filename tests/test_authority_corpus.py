from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "spec" / "golden-authority-v2"
R0_CORPUS = ROOT / "spec" / "golden"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_authority_manifest_authenticates_every_file_and_table():
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "trajecta.golden-authority-manifest/v1"
    actual = {
        path.relative_to(CORPUS).as_posix(): sha256(path)
        for path in CORPUS.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    }
    for table in sorted((ROOT / "memory_core/tables").glob("*.json")):
        actual[f"tables/{table.name}"] = sha256(table)
    assert actual == manifest["files"]
    assert manifest["legacy_v4_sha256"] == sha256(
        ROOT / "spec/golden/identity-open/store.sqlite3"
    )


def test_immutable_r0_manifest_still_authenticates_the_v4_corpus():
    manifest = json.loads((R0_CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))
    actual = {
        path.relative_to(R0_CORPUS).as_posix(): sha256(path)
        for path in R0_CORPUS.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    }
    for table in sorted((ROOT / "memory_core/tables").glob("*.json")):
        actual[f"tables/{table.name}"] = sha256(table)
    assert actual == manifest["files"]


def test_every_authority_scenario_has_the_four_contract_files():
    scenarios = sorted(path for path in CORPUS.iterdir() if path.is_dir())
    assert len(scenarios) == 35
    for scenario in scenarios:
        assert {path.name for path in scenario.iterdir()} == {
            "script.json", "store.sqlite3", "dump.json", "cases.jsonl"
        }
        script = json.loads((scenario / "script.json").read_text(encoding="utf-8"))
        assert script["actions"]
        cases = (scenario / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(cases) == 1
        assert json.loads(cases[0])["label"] == scenario.name
