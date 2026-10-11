import json
import os
from pathlib import Path
import shutil
import pytest
from tools.r3.rehearsal import rehearse, validate_source, validate_owner_root
from tools.r3.common import snapshot

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fixture,state", [
    ("golden-writes-v5/bootstrap/store.sqlite3", "ready"),
    ("golden-cli-v1/migration-v2/fixture/store.sqlite3", "legacy-v2"),
    ("golden-cli-v1/migration-v3/fixture/store.sqlite3", "legacy-v3"),
    ("golden-cli-v1/migration-v4/fixture/store.sqlite3", "legacy-v4"),
    ("golden/foreign-app/store.sqlite3", "incompatible"),
    ("golden/future-schema/store.sqlite3", "incompatible"),
    ("golden/empty-file/store.sqlite3", "unknown"),
])
def test_synthetic_rehearsal_branches_source_immutable_and_report_redacted(tmp_path, fixture, state):
    source_fixture = ROOT / "spec" / fixture
    source = tmp_path / "input.sqlite3"
    shutil.copy2(source_fixture, source)
    profile = tmp_path / "profile.json"
    shutil.copy2(ROOT / "trajecta_identity/profiles/example/profile.json", profile)
    before = snapshot(tmp_path)
    report = rehearse(tmp_path, source, profile, ["synthetic-cue-private"])
    assert report["state"] == state
    after = snapshot(tmp_path)
    for key in before: assert after[key] == before[key]
    raw = json.dumps(report)
    for private in ["synthetic-cue-private", str(tmp_path), "record_id", "summary", "title"]:
        assert private not in raw
    if state in {"ready", "legacy-v2", "legacy-v3", "legacy-v4"}:
        assert report["result"] == "passed"
        assert any(s["step"] == "decay" for s in report["steps"])
        assert all(all(s["doctor_checks"].values()) for s in report["steps"] if s.get("doctor_checks"))
    else: assert report["result"] == "refused"


@pytest.mark.parametrize("kind", ["sidecar", "header"])
def test_wal_source_stops_without_opening_original(tmp_path, kind):
    source = tmp_path / "input.sqlite3"
    shutil.copy2(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", source)
    if kind == "sidecar":
        source.with_name(source.name + "-wal").write_bytes(b"synthetic-wal")
    else:
        raw = bytearray(source.read_bytes()); raw[18:20] = b"\x02\x02"; source.write_bytes(raw)
    profile = tmp_path / "profile.json"
    shutil.copy2(ROOT / "trajecta_identity/profiles/example/profile.json", profile)
    before = snapshot(tmp_path)
    report = rehearse(tmp_path, source, profile, [])
    assert report["state"] == "wal" and report["result"] == "refused"
    after = snapshot(tmp_path)
    for key in before: assert after[key] == before[key]


def test_owner_root_refuses_any_repository():
    with pytest.raises(ValueError): validate_owner_root(ROOT / ".isolation")


def test_source_containment_regular_file_and_single_link(tmp_path):
    folder = tmp_path / "selected"; folder.mkdir()
    source = tmp_path / "outside.sqlite3"; source.write_bytes(b"synthetic")
    with pytest.raises(ValueError): validate_source(folder, source)
    alias = folder / "alias.sqlite3"
    try: alias.symlink_to(source)
    except OSError: pytest.skip("host cannot create a synthetic symlink")
    with pytest.raises(ValueError): validate_source(folder, alias)
    alias.unlink(); os.link(source, alias)
    with pytest.raises(ValueError): validate_source(folder, alias)
    with pytest.raises(ValueError): validate_source(folder, folder)


def test_work_profile_refused_before_any_further_copy(tmp_path):
    source = tmp_path / "input.sqlite3"
    shutil.copy2(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", source)
    profile = tmp_path / "profile.json"
    profile.write_text('{"work_root":"synthetic-out-of-scope"}')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="outside identity rehearsal scope"):
        rehearse(tmp_path, source, profile, [])
    assert snapshot(tmp_path) == before


def test_cross_reads_use_the_same_input_when_decay_differs_within_whitelist(tmp_path, monkeypatch):
    """Model permitted platform float drift at the dump-reader seam, on copies only."""
    import copy
    import tools.r3.rehearsal as runner
    source = tmp_path / "input.sqlite3"
    shutil.copy2(ROOT / "spec/golden-writes-v5/phase/store.sqlite3", source)
    profile = tmp_path / "profile.json"
    shutil.copy2(ROOT / "trajecta_identity/profiles/example/profile.json", profile)
    drifted = set()
    original_copy, original_run, original_dump = runner.copy_store, runner.run_case, runner.schema_and_dump
    def copied(root, input_copy, target):
        original_copy(root, input_copy, target)
        if input_copy in drifted: drifted.add(target)
    def called(runtime, root, case):
        result = original_run(runtime, root, case)
        if runtime == "ts" and case["argv"][-1] == "decay": drifted.add(root / "store.sqlite3")
        return result
    def dumped(path):
        schema, dump = original_dump(path)
        if path not in drifted: return schema, dump
        dump = copy.deepcopy(dump)
        op = next(r for r in reversed(dump["tables"]["memory_operations_v3"]) if r["idempotency_key"].startswith("maintenance:decay:"))
        details = json.loads(op["details_json"])
        adjustment = details["adjustments"][0]
        adjustment["new_value"] += 1e-12
        op["details_json"] = json.dumps(details, ensure_ascii=False)
        revision = next(r["revision_id"] for r in dump["tables"]["memory_revisions_v3"] if r["record_id"] == adjustment["record_id"])
        row = next(r for r in dump["tables"]["memory_telemetry_v3"] if r["revision_id"] == revision)
        value = float.fromhex(row["accessibility"]["hex"]) + 1e-12
        row["accessibility"] = {"repr": repr(value), "hex": value.hex()}
        return schema, dump
    monkeypatch.setattr(runner, "copy_store", copied)
    monkeypatch.setattr(runner, "run_case", called)
    monkeypatch.setattr(runner, "schema_and_dump", dumped)
    before = snapshot(tmp_path)
    assert rehearse(tmp_path, source, profile, [])["result"] == "passed"
    after = snapshot(tmp_path)
    for key in before: assert after[key] == before[key]
