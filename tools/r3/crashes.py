"""Real SIGKILL/TerminateProcess at actual transaction boundaries, both runtimes."""
import argparse
import base64
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import copy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import isolated_env, compare_dumps, schema_and_dump, assert_decay_margin
from tools.r3.run import command, run_case, validate_case, case_for_step
from tools.r3.plans import step
from tools.r3.replay import replay_units


def intake_case(**updates):
    proposal = {"operation_type": "create", "record_id": "g2-crash", "record_class": "event", "domain": "phase",
                "actor": "synthetic", "reason": "R3 crash classification", "logic": "synthetic fixture",
                "truth_basis": "synthetic provenance", "changes": {"title": "Synthetic intake", "summary": "synthetic"},
                "evidence": [{"source_ref": "synthetic:g2-crash", "content_summary": "synthetic", "confidence": 0.9}],
                "idempotency_key": "synthetic:g2-crash"}
    proposal.update(updates)
    return {"surface": "kernel", "call": "runtime_submit", "arguments": {"proposal": proposal}}


def kill_case(runtime, root, case):
    env = isolated_env(root)
    case = {**case, "root": str(root), "env": env}
    validate_case(root, case)
    child = subprocess.Popen(command(runtime), cwd=root, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen = queue.Queue()
    reader = threading.Thread(target=lambda: seen.put(child.stderr.readline()), daemon=True)
    reader.start()
    try:
        child.stdin.write(json.dumps(case).encode() + b"\n")
        child.stdin.flush()
        marker = seen.get(timeout=15)
        assert marker == b"R3-CRASH-READY\n", marker
        assert child.poll() is None
        child.kill()
        child.communicate(timeout=15)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)
        reader.join(timeout=1)


def ready(runtime, root):
    output = run_case(runtime, root, case_for_step(root, step(runtime, "cli", "doctor"), 0, {}))
    assert output["exit"] == 0, output
    result = json.loads(output["stdout"])
    assert result["state"] == "ready" and result["passed"]
    assert len(result["checks"]) == 7 and all(result["checks"].values())


def run(parent):
    fixture_root = parent / "fixture"
    isolated_env(fixture_root)
    shutil.copyfile(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", fixture_root / "store.sqlite3")
    seed = {"surface": "kernel", "call": "log_phase", "arguments": {
        "event_id": "base", "title": "Unique crash cue", "summary": "synthetic baseline", "cues": ["unique crash cue"],
    }}
    assert run_case("py", fixture_root, seed)["exit"] == 0
    fixture = fixture_root / "store.sqlite3"
    cases = [
        ("create", "transaction", {"surface": "kernel", "call": "create_current", "arguments": {
            "record_id": "crash-new", "record_class": "belief", "domain": "fact", "title": "Crash",
            "actor": "synthetic", "reason": "R3", "evidence": {"source_ref": "synthetic:r3"}, "idempotency_key": "crash-new"}}),
        ("revise", "transaction", {"surface": "kernel", "call": "revise", "arguments": {
            "record_id": "phase:base", "operation_type": "refine", "actor": "synthetic", "reason": "R3",
            "evidence": {"source_ref": "synthetic:r3"}, "idempotency_key": "crash-revise", "changes": {"summary": "refined"}}}),
        ("relation", "transaction", {"surface": "kernel", "call": "add_relation", "arguments": {
            "relation_id": "crash-rel", "from_record_id": "phase:base", "to_record_id": "core",
            "relation_type": "supports", "actor": "synthetic", "reason": "R3", "idempotency_key": "crash-rel"}}),
        ("g10", "g10", {"surface": "kernel", "call": "log_phase", "arguments": {
            "event_id": "crash", "title": "Crash phase", "summary": "synthetic", "follows": ["phase:base"], "cues": ["crash cue"]}}),
        ("g10-fact", "g10", {"surface": "kernel", "call": "log_fact", "arguments": {
            "fact_id": "crash", "title": "Crash fact", "summary": "synthetic", "caused_by": ["phase:base"], "cues": ["crash cue"]}}),
        ("invalidate", "transaction", {"surface": "kernel", "call": "invalidate", "arguments": {
            "record_id": "phase:base", "actor": "synthetic", "reason": "R3", "evidence": {"source_ref": "synthetic:r3"},
            "idempotency_key": "crash-invalidate"}}),
        ("maintenance", "transaction", {"surface": "kernel", "call": "maintenance", "arguments": {
            "run_id": "crash-maintenance", "actor": "synthetic", "reason": "R3", "adjustments": [
                {"record_id": "phase:base", "field": "accessibility", "old_value": 0.6, "new_value": 0.7}]}}),
        ("g12", "g12", {"surface": "kernel", "call": "retrieve", "arguments": {"cue": "unique crash cue", "track": True, "limit": 2}}),
        ("g9", "g9", {"surface": "kernel", "call": "decay", "arguments": {"now": "2026-10-30T00:00:00+00:00"}}),
    ]
    cases += [
        ("g2-materialized", "g2", intake_case()),
        ("g2-held", "g2", intake_case(unresolved_conflict=True)),
        ("g2-no-op", "g2", intake_case(operation_type="refine", record_id="phase:base", changes={})),
    ]
    fixtures = {}
    recall_root = parent / "recall-fixture"
    isolated_env(recall_root)
    shutil.copy2(fixture, recall_root / "store.sqlite3")
    second_hit = {**seed, "arguments": {**seed["arguments"], "event_id": "base-two"}}
    assert run_case("py", recall_root, second_hit)["exit"] == 0
    fixtures["g12"] = recall_root / "store.sqlite3"
    proposal_case = {"surface": "kernel", "call": "identity_core_propose", "arguments": {
        "reason": "synthetic proposal", "phase_context": {"model": "synthetic"}}}
    cases.append(("authority-propose", "transaction", proposal_case))
    authority_root = parent / "authority-fixture"
    isolated_env(authority_root)
    shutil.copy2(fixture, authority_root / "store.sqlite3")
    proposal = json.loads(run_case("py", authority_root, proposal_case)["stdout"])
    proposal_fixture = parent / "proposal.sqlite3"
    shutil.copy2(authority_root / "store.sqlite3", proposal_fixture)
    issue_case = {"surface": "kernel", "call": "owner_approve_core", "arguments": {
        "proposal_id": proposal["proposal_id"], "outcome": "apply"}}
    cases.append(("authority-issue", "transaction", issue_case))
    fixtures["authority-issue"] = proposal_fixture
    receipt = json.loads(run_case("py", authority_root, issue_case)["stdout"])
    consume_case = {"surface": "kernel", "call": "identity_core_apply", "arguments": {"receipt_id": receipt["receipt_id"]}}
    cases.append(("authority-consume", "authority-revision", consume_case))
    fixtures["authority-consume"] = authority_root / "store.sqlite3"
    assert_decay_margin(fixture, "2026-10-30T00:00:00+00:00")
    outcomes = []
    for name, point, case in cases:
        scenario_fixture = fixtures.get(name, fixture)
        clean = parent / (name + "-clean")
        isolated_env(clean)
        shutil.copy2(scenario_fixture, clean / "store.sqlite3")
        normal = run_case("py", clean, {**case, "capture": True})
        assert normal["exit"] == 0 and json.loads(normal["stdout"]) is not None, normal
        units = normal["units"]
        if name == "g12":
            assert [unit["call"] for unit in units] == ["record_access", "record_access", "apply_maintenance"]
        # Only the oracle's actual committed units define boundaries; skipped links add none.
        boundaries = [0] if point in {"transaction", "authority-revision"} else range(1, len(units) + 1)
        if name.startswith("g2-"):
            calls = [unit["call"] for unit in units]
            expected_calls = ["_capture_evidence", "_insert_intake", "create_current", "_link_evidence", "_decide_intake"] if name == "g2-materialized" else ["_capture_evidence", "_insert_intake", "_decide_intake"]
            assert calls == expected_calls, calls
        for boundary in boundaries:
            for runtime, retry in (("py", "ts"), ("ts", "py")):
                actual = parent / f"{name}-{boundary}-{runtime}"
                expected = parent / f"{name}-{boundary}-{runtime}-expected"
                isolated_env(actual); isolated_env(expected)
                shutil.copy2(scenario_fixture, actual / "store.sqlite3")
                oracle = replay_units(scenario_fixture, expected, units[:boundary])
                fault = {"crash": point} if boundary == 0 else {"crash_after_unit": boundary}
                kill_case(runtime, actual, {**case, **fault})
                ready(runtime, actual)
                ws, wd = schema_and_dump(oracle)
                gs, gd = schema_and_dump(actual / "store.sqlite3")
                assert ws == gs
                compare_dumps(wd, gd, decay=name == "g9")
                assert not (actual / "store.activation.json").exists()
                before_accesses = len(gd["tables"]["memory_access_v3"])
                a = run_case("py", expected, case)
                b = run_case(retry, actual, case)
                assert a == b, (name, boundary, runtime, a, b)
                ws, wd = schema_and_dump(oracle)
                gs, gd = schema_and_dump(actual / "store.sqlite3")
                assert ws == gs
                compare_dumps(wd, gd, decay=name == "g9")
                if name == "g12":
                    assert len(gd["tables"]["memory_access_v3"]) > before_accesses
                    assert len(gd["tables"]["memory_access_v3"]) > len(schema_and_dump(clean / "store.sqlite3")[1]["tables"]["memory_access_v3"])
                ready(retry, actual)
                assert not any((actual / ("store.sqlite3" + suffix)).exists() for suffix in ("-wal", "-shm", "-journal"))
                outcomes.append(f"{name}:{boundary}:{runtime}→{retry}")
    return {"crashes": len(outcomes), "cases": outcomes}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="r3-crash-") as folder:
        print(json.dumps(run(Path(folder).resolve())))
