"""Frozen CLI tables and fail-closed expected-byte rendering."""
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

import pytest

from tools.golden_cli.generate import table_values
from tools.golden_cli.tokens import template, render

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "spec/golden-cli-v1"


def test_manifest_inventory_and_hashes():
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text())
    actual = {p.relative_to(CORPUS).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in CORPUS.rglob("*") if p.is_file() and p.name != "MANIFEST.json"}
    assert actual == manifest["files"]
    assert {"P10-read-only-doctor", "P13-public-cli-errors", "P14-backup-alias"} <= set(manifest["oracle_semantics"])


@pytest.mark.skipif(sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0",
                    reason="CLI truth tables require Python 3.11 / Unicode 14.0.0")
def test_cli_tables_exhaustive_rederivation():
    for name, values in table_values().items():
        data = json.loads((CORPUS / (name + ".json")).read_text())
        digest = data.pop("sha256")
        assert hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest() == digest
        assert data["unicode"] == "14.0.0"
        for key, value in values.items(): assert data[key] == value
    # Python strip whitespace and int whitespace differ at ASCII U+001C..1F.
    assert int("\u20031\u3000") == 1
    with pytest.raises(ValueError): int("\x1c1\x1f")


@pytest.mark.parametrize("file", ["stdout", "stderr", "data/.last-profile", "argv.json", "env.json"])
@pytest.mark.parametrize("suffix", [b"X", b"9"])
def test_generator_refuses_prefix_boundary(file, suffix):
    with pytest.raises(ValueError, match="boundary"):
        template(b"/fixture"+suffix, file=file, root="/fixture", origin="", context="raw-text")
    with pytest.raises(ValueError, match="boundary"):
        template(b"http://127.0.0.1:123"+suffix, file=file, root="/fixture",
                 origin="http://127.0.0.1:123", context="raw-text")


@pytest.mark.parametrize("context", ["raw-text", "json-string"])
def test_template_roundtrip_and_fresh_origin(context):
    raw = b'"/fixture/store.sqlite3 http://127.0.0.1:123/profile.json"'
    encoded, entries = template(raw, file="stdout", root="/fixture", origin="http://127.0.0.1:123", context=context, separator="/")
    expected = render(encoded, entries, file="stdout", root="/fresh", origin="http://127.0.0.1:456", separator="/")
    assert expected == b'"/fresh/store.sqlite3 http://127.0.0.1:456/profile.json"'
    assert expected != b'"/freshX/store.sqlite3 http://127.0.0.1:4569/profile.json"'
    with pytest.raises(ValueError): render(encoded, entries[:-1], file="stdout", root="/fresh", origin="http://127.0.0.1:456", separator="/")


def test_windows_json_explicit_context():
    raw = b'{"db":"C:\\\\fixture\\\\store.sqlite3"}'
    encoded, entries = template(raw, file="stdout", root="C:\\fixture", origin="", context="json-string", separator="\\")
    assert b"{{ROOT}}/store.sqlite3" in encoded
    assert render(encoded, entries, file="stdout", root="D:\\new", origin="", separator="\\") == b'{"db":"D:\\\\new\\\\store.sqlite3"}'


@pytest.mark.parametrize("raw,file", [(b"{{ROOT}}", "stdout"), (b"/fixture/x", "profile.json"), (b"/FIXTURE/x", "stderr")])
def test_forbidden_tokens_and_other_forms(raw, file):
    with pytest.raises(ValueError): template(raw, file=file, root="/fixture", origin="", context="raw-text")


def test_cli_corpus_covers_the_declared_matrix_and_preserves_refusal_bytes():
    from tools.golden_cli.generate import scenario_matrix
    scenarios = {p.name for p in CORPUS.iterdir() if p.is_dir()}
    assert scenarios == {item["name"] for item in scenario_matrix()}
    for name in scenarios:
        directory = CORPUS / name
        result = json.loads((directory / "expected.json").read_text())
        if name.startswith("doctor-"):
            assert "data/.last-profile" not in result["tree"]
            for relative, entry in result["tree"].items():
                if entry["kind"] == "store":
                    assert entry["unchanged_sha256"] == hashlib.sha256((directory / "fixture" / relative).read_bytes()).hexdigest()
        if name in {"status-missing", "retrieve-missing", "timeline-missing", "core-proposals-missing", "work-missing"}:
            assert not any(path.startswith("store.sqlite3") for path in result["tree"])
        for token in json.loads((directory / "tokens.json").read_text()):
            assert token["file"] in {"stdout", "stderr", "data/.last-profile", "argv.json", "env.json"}


@pytest.mark.parametrize("raw", [b'"/fixture/../fixture/store"', b'"/fixture/./store"', b'"C:\\fixture\\store"'])
def test_unresolved_or_partly_escaped_forms_are_refused(raw):
    root = "C:\\fixture" if b"C:" in raw else "/fixture"
    with pytest.raises(ValueError): template(raw, file="stdout", root=root, origin="", context="json-string")


@pytest.mark.parametrize("file", ["stdout", "stderr", "data/.last-profile", "argv.json", "env.json"])
def test_replay_refuses_a_token_followed_by_an_extra_character(file):
    entry = {"file": file, "byte_offset": 0, "token": "{{ROOT}}", "context": "raw-text"}
    with pytest.raises(ValueError): render(b"{{ROOT}}X", [entry], file=file, root="/fixture", origin="")


def test_database_classes_are_explicit_and_cannot_relax_unchanged_fixtures():
    kinds = set()
    for directory in CORPUS.iterdir():
        if not directory.is_dir(): continue
        result = json.loads((directory / "expected.json").read_text())
        for relative, entry in result["databases"].items():
            kind = entry["class"]; kinds.add(kind)
            fixture = directory / "fixture" / relative
            if kind == "absent": assert relative not in result["tree"]
            elif kind == "unchanged":
                assert entry["oracle_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
            elif kind == "written":
                assert set(entry["schema"]) == {"application_id", "user_version", "sqlite_master"}
                assert relative in result["dumps"]
                assert not any(relative + suffix in result["tree"] for suffix in ("-wal", "-shm", "-journal"))
            elif kind == "backup":
                assert entry["oracle_sha256"] == hashlib.sha256((directory / "fixture" / entry["source"]).read_bytes()).hexdigest()
            else: pytest.fail(f"unrecognized database class {kind}")
    assert kinds == {"absent", "unchanged", "written", "backup"}
