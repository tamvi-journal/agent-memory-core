"""Owner-local R3 M runner. Input is an immutable owner-made copy, never a live store.

Only main accepts owner paths; tests call rehearse on synthetic isolated fixtures.
Reports contain static labels, states, counts, booleans and SHA-256 only.
"""
import argparse
import json
from pathlib import Path
import shutil
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import (contained, isolated_env, snapshot, sha, schema_and_dump,
                             compare_trees, expected_for_run, assert_decay_margin)
from tools.r3.run import run_case

CLOCK = "2026-10-07T00:00:00+00:00"
SUFFIXES = ("-wal", "-shm", "-journal")
TABLES = {"records": "memory_records_v3", "revisions": "memory_revisions_v3",
          "evidence": "memory_evidence_v3", "relations": "memory_relation_events_v4",
          "cues": "memory_cues_v3", "receipts": "memory_owner_receipts_v5"}


def validate_owner_root(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir(): raise ValueError("selected root must be a directory")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("rehearsal root must be outside every repository")
    return root


def validate_source(root, source):
    path = contained(Path(root), Path(root) / source)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("input must be a regular file with one link")
    return path


def input_files(source):
    return [source, source.with_suffix(".activation.json"),
            *(source.with_name(source.name + suffix) for suffix in SUFFIXES)]


def input_state(root, source):
    result = {}
    for index, path in enumerate(input_files(source)):
        contained(root, path)
        if path.exists():
            path = validate_source(root, path)
            info = path.stat()
            result[index] = (sha(path.read_bytes()), info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return result


def copy_store(root, source, target):
    contained(root, target)
    shutil.copy2(source, target)
    for suffix in SUFFIXES:
        path = source.with_name(source.name + suffix)
        if path.exists(): shutil.copy2(validate_source(root, path), target.with_name(target.name + suffix))
    side = source.with_suffix(".activation.json")
    if side.exists(): shutil.copy2(validate_source(root, side), target.with_suffix(".activation.json"))


def digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def rehearse(root, source, profile, cues):
    """Internal core; main adds the outside-repositories owner guard."""
    root = Path(root).resolve(strict=True)
    source = validate_source(root, source)
    profile = validate_source(root, profile)
    profile_bytes = profile.read_bytes()
    profile_data = json.loads(profile_bytes)
    if profile_data.get("work_root"):
        raise ValueError("work stores are outside identity rehearsal scope")
    if not isinstance(cues, list) or any(not isinstance(cue, str) for cue in cues):
        raise ValueError("cues must be a list of strings")
    original = input_state(root, source)
    report = {"schema": "trajecta.rehearsal-report/v1", "state": "unknown", "result": "refused", "steps": []}
    work = contained(root, root / ("rehearsal-" + uuid.uuid4().hex))
    work.mkdir()
    counter = 0

    def record(runtime, operation, state, result, database, cue_index=None):
        entry = {"runtime": runtime, "step": operation, "state": state, "exit": result["exit"],
                 "stdout_sha256": sha(result["stdout"]), "stderr_sha256": sha(result["stderr"])}
        if cue_index is not None: entry.update(cue_index=cue_index, count=1)
        if operation == "doctor" and result["exit"] == 0:
            doc = json.loads(result["stdout"])
            if state == "ready":
                assert doc["passed"] and len(doc["checks"]) == 7 and all(doc["checks"].values())
                entry["doctor_checks"] = doc["checks"]
        if state == "ready" and database.exists():
            if operation != "doctor":
                proof = run_case(runtime, database.parent, {"surface": "cli", "clock": CLOCK,
                    "argv": ["--profile", "example", "--db", database.name, "doctor"]})
                doc = json.loads(proof["stdout"])
                assert proof["exit"] == 0 and doc["passed"] and len(doc["checks"]) == 7 and all(doc["checks"].values())
                entry["doctor_checks"] = doc["checks"]
            schema, dump = schema_and_dump(database)
            entry.update(schema_sha256=digest(schema), dump_sha256=digest(dump),
                         counts={key: len(dump["tables"].get(table, [])) for key, table in TABLES.items()})
        report["steps"].append(entry)

    def pair(operation, args=(), sources=None, state="ready", cue_index=None, expect_exit=0):
        nonlocal counter
        roots = [work / f"step-{counter}-{runtime}" for runtime in ("py", "ts")]
        counter += 1
        before, outputs, paths = [], [], []
        for runtime, child_root, input_copy in zip(("py", "ts"), roots, sources or (source, source)):
            env = isolated_env(child_root)
            Path(env["TRAJECTA_IDENTITY_PROFILES"]).joinpath("example/profile.json").write_bytes(profile_bytes)
            path = child_root / "store.sqlite3"
            copy_store(root, input_copy, path)
            if operation == "decay": assert_decay_margin(path, CLOCK)
            before.append(snapshot(child_root))
            output = run_case(runtime, child_root, {"surface": "cli", "clock": CLOCK,
                "argv": ["--profile", "example", "--db", "store.sqlite3", operation, *args]})
            paths.append(path); outputs.append(output)
        assert outputs[0]["exit"] == outputs[1]["exit"]
        if expect_exit is not None: assert outputs[0]["exit"] == expect_exit
        for boundary in ("stdout", "stderr"):
            assert expected_for_run(outputs[0][boundary], boundary, *roots,
                                    "json-string" if boundary == "stdout" else "raw-text") == outputs[1][boundary]
        compare_trees(*roots, *before, decay=operation == "decay")
        actual_state = state
        if operation == "doctor":
            if not outputs[0]["stdout"]:
                if outputs[0]["stderr"].startswith(b"IncompatibleJournalMode:"):
                    actual_state = "wal"
                else:
                    assert outputs[0]["stderr"].startswith(b"SchemaVersionError:")
                    actual_state = "incompatible"
            else: actual_state = json.loads(outputs[0]["stdout"])["state"]
        if operation == "migrate-to" and outputs[0]["exit"] == 0 and "--dry-run" not in args:
            actual_state = "ready"
            paths = [path.with_name(args[0]) for path in paths]
        if actual_state == "ready":
            from tools.r3.common import compare_dumps
            schemas_dumps = [schema_and_dump(path) for path in paths]
            assert schemas_dumps[0][0] == schemas_dumps[1][0]
            compare_dumps(schemas_dumps[0][1], schemas_dumps[1][1], decay=operation == "decay")
        for runtime, output, path in zip(("py", "ts"), outputs, paths):
            record(runtime, operation, actual_state, output, path, cue_index)
        return paths, outputs, actual_state

    def reads(sources):
        pair("doctor", sources=sources)
        for operation, args in [("status", []), ("timeline", ["--limit", "1000"]), ("core-proposals", [])]:
            pair(operation, args, sources=sources)
        for index, cue in enumerate(cues):
            pair("retrieve", [cue, "--readonly"], sources=sources, cue_index=index)

    try:
        is_wal = (2 in source.read_bytes()[:20][18:20] or
                  any(source.with_name(source.name + s).exists() for s in ("-wal", "-shm")))
        _, _, state = pair("doctor", state="unknown", expect_exit=1 if is_wal else None)
        report["state"] = state
        if state not in {"ready", "legacy-v2", "legacy-v3", "legacy-v4"}: return report
        if state.startswith("legacy-"):
            pair("migrate-to", ["dry.sqlite3", "--dry-run"], state=state)
            paths, _, _ = pair("migrate-to", ["target.sqlite3"], state=state)
            sources = paths
            for path in paths:
                backup = path.with_name("target.sqlite3." + state.removeprefix("legacy-") + ".bak")
                assert backup.read_bytes() == source.read_bytes()
                assert backup.stat().st_mtime_ns // 1000 == source.stat().st_mtime_ns // 1000
        else:
            _, outputs, _ = pair("migrate-to", ["target.sqlite3"], expect_exit=1)
            assert outputs[0]["stderr"] == b"MigrationRequiredError: store state 'ready' is not an explicit migration source\n"
            sources = [source, source]
        reads(sources)
        cue = cues[0] if cues else ""
        pair("retrieve", [cue], sources=sources, cue_index=0 if cues else None)
        pair("decay", sources=sources)
        if state.startswith("legacy-"):
            reads(list(reversed(sources)))
        else:
            tracked, _, _ = pair("retrieve", [cue], sources=sources, cue_index=0 if cues else None)
            decayed, _, _ = pair("decay", sources=tracked)
            reads(list(reversed(decayed)))
        report["result"] = "passed"
        return report
    finally:
        assert input_state(root, source) == original, "immutable source law violated"


def main():
    parser = argparse.ArgumentParser(description="Owner-only compatibility rehearsal on contained copies")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--cues", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = validate_owner_root(args.root)
        cue_file = validate_source(root, args.cues)
        report_path = contained(root, root / "rehearsal-report.json")
        if report_path.exists(): raise ValueError("report already exists")
        report = rehearse(root, args.source, args.profile, json.loads(cue_file.read_bytes()))
        report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"result": report["result"], "state": report["state"], "steps": len(report["steps"])}))
        return 0 if report["result"] == "passed" else 1
    except Exception as error:
        # Never print exception messages, paths, record text or private cues.
        print(json.dumps({"result": "failed", "category": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
