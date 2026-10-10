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
        "P15-evidence-metadata",
        "P16-store-busy", "P17-sequence-allocation", "P17-initialization-guard",
        "P18-intake-only-replay", "R3-backup-microsecond-domain",
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
        "evidence-metadata-p15", "evidence-replay-p15",
        "evidence-intake-defaults-p15", "relation-missing-endpoint-r3",
    }


def test_p15_corpus_covers_every_field_kind_and_replay_precedence():
    script = json.loads((CORPUS / "evidence-metadata-p15/script.json").read_text())
    actions = script["actions"][1:]
    assert len(actions) == 66
    fields = {
        "evidence_type", "source_ref", "source_family", "independence_group",
        "captured_at", "actor", "surface", "model_family", "content_summary",
        "privacy_class", "identity_version",
    }
    for field in fields:
        values = [a["arguments"]["evidence"][field] for a in actions if field in a["arguments"]["evidence"]]
        assert [type(value) for value in values] == [type(None), bool, int, float, list, dict]
    cases = json.loads((CORPUS / "evidence-metadata-p15/cases.jsonl").read_text())["results"][1:]
    assert all(c["result"]["error"] == "ValueError" and c["result"]["store_bytes_unchanged"] and c["result"]["no_operation"] for c in cases)
    replay = json.loads((CORPUS / "evidence-replay-p15/cases.jsonl").read_text())["results"]
    assert replay[1]["result"] == replay[2]["result"]
