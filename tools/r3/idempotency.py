"""Deterministic P18 public-intake gates; only synthetic contained databases."""
import base64
import copy
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from tools.r3.common import isolated_env, schema_and_dump
from tools.r3.crashes import intake_case, ready
from tools.r3.replay import replay_units
from tools.r3.run import command, run_case, validate_case

ROOT = Path(__file__).resolve().parents[2]


def start(runtime, root, case, **gate):
    env = isolated_env(root)
    release = root / (gate.pop("label") + ".release")
    case = {**case, **gate, "root": str(root), "env": env, "p18_release": str(release), "capture": True}
    validate_case(root, case)
    child = subprocess.Popen(command(runtime), cwd=root, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen = queue.Queue()
    def markers():
        for _ in range(2 if gate.get("same_key_gate_role") == "victim" else 1):
            seen.put(child.stderr.readline())
    threading.Thread(target=markers, daemon=True).start()
    child.stdin.write(json.dumps(case).encode() + b"\n")
    child.stdin.flush()
    child.release_gate = release
    return child, seen


def finish(child):
    child.release_gate.write_bytes(b"R")
    out, err = child.communicate(timeout=20)
    assert child.returncode == 0 and not err, (out, err)
    result = json.loads(out)
    return {**result, "stdout": base64.b64decode(result["stdout"]), "stderr": base64.b64decode(result["stderr"])}


def output(result):
    return {key: result[key] for key in ("exit", "stdout", "stderr")}


def intake(root, loser, winner, variant, seed=0):
    env = isolated_env(root)
    fixture = ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3"
    shutil.copy2(fixture, root / "store.sqlite3")
    case = intake_case(idempotency_key=f"shared:{seed}", record_id=f"shared-{seed}")
    case.update(clock="2026-10-01T00:00:00+00:00")
    other = copy.deepcopy(case)
    other["clock"] = "2026-10-02T00:00:00+00:00"
    if variant == "different":
        other["arguments"]["proposal"]["changes"]["summary"] = "different proposal"
    children = []
    try:
        loser_invoked = time.monotonic_ns()
        first, seen = start(loser, root, case, label="loser", intake_pause_after="_capture_evidence")
        children.append(first)
        assert seen.get(timeout=15) == b"R3-P18-_capture_evidence\n"
        winner_invoked = time.monotonic_ns()
        if variant == "received":
            second, seen = start(winner, root, other, label="winner", intake_pause_after="_insert_intake")
            children.append(second)
            assert seen.get(timeout=15) == b"R3-P18-_insert_intake\n"
        else:
            winning = run_case(winner, root, {**other, "capture": True})
            assert winning["exit"] == 0 and winning["stdout"].startswith(b"{"), winning
            winner_returned = time.monotonic_ns()
        expected = run_case("py", root, case)
        before = schema_and_dump(root / "store.sqlite3")
        lost = finish(first)
        loser_returned = time.monotonic_ns()
        assert output(lost) == expected, (variant, lost, expected)
        assert [u["call"] for u in lost["units"]] == ["_capture_evidence"], lost
        assert schema_and_dump(root / "store.sqlite3") == before
        if variant == "received":
            assert json.loads(lost["stdout"])["status"] == "received"
            winning = finish(second)
            winner_returned = time.monotonic_ns()
        elif variant == "different":
            assert lost["stdout"] == b"ValueError: idempotency_key already exists with a different proposal"
        # Commit-unit witness: loser evidence, followed by the winner's actual units.
        serial = replay_units(fixture, root / "serial", lost["units"] + winning["units"])
        assert schema_and_dump(serial) == schema_and_dump(root / "store.sqlite3")
        (root / "shared-key-history.json").write_text(json.dumps({
            "operations": [
                {"runtime": loser, "invoke_ts": loser_invoked, "return_ts": loser_returned, "units": lost["units"]},
                {"runtime": winner, "invoke_ts": winner_invoked, "return_ts": winner_returned, "units": winning["units"]}],
            "python_serial_witness": lost["units"] + winning["units"]}, indent=2) + "\n")
        ready("py", root)
        ready("ts", root)
        assert not any((root / ("store.sqlite3" + s)).exists() for s in ("-wal", "-shm", "-journal"))
        return schema_and_dump(root / "store.sqlite3"), {"variant": variant, "seed": seed,
                "loser": loser, "winner": winner, "units": len(lost["units"] + winning["units"])}
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=20)


PUBLIC_SITES = (
    "create", "revise", "invalidate", "maintenance", "add-relation", "retract-relation",
    "core-proposal", "core-apply", "retract-consumer", "legacy-close-consumer",
)


def fixture_case(root, site):
    isolated_env(root)
    path = root / "store.sqlite3"
    if site == "legacy-close-consumer":
        from memory_core import MemoryStore
        from trajecta_identity import IdentityMemory, load_profile
        from tools.golden_authority.generate import TTY, short
        from tools.golden.generate import clock_context
        source = root / "legacy.sqlite3"
        shutil.copy2(ROOT / "spec/golden/identity-open/store.sqlite3", source)
        MemoryStore(source).migrate_to(path, backup_path=root / "legacy-backup.sqlite3")
        with clock_context():
            mem = IdentityMemory(load_profile(root / "profiles/example"), path, surface="golden")
            with mem.store.connect(readonly=True) as conn:
                discussion = conn.execute("SELECT * FROM memory_relation_events_v4 WHERE relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1").fetchone()
            tty = TTY("CLOSE " + short(discussion["relation_event_id"]))
            receipt = mem.issue_legacy_close_receipt(note="R3 same-key close", stdin=tty, stdout=tty)
        return {"surface": "kernel", "call": "identity_close_legacy_discussion", "arguments": {"receipt_id": receipt["receipt_id"]}}, "memory_receipt_consumptions_v5"
    shutil.copy2(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", path)
    phase = {"surface": "kernel", "call": "log_phase", "arguments": {"event_id": "base", "title": "Base", "summary": "synthetic"}}
    assert run_case("py", root, phase)["stdout"].startswith(b"{")
    base = {"actor": "synthetic", "reason": "R3 same-key", "evidence": {"source_ref": "synthetic:same-key"}, "idempotency_key": "same-key"}
    table = "memory_operations_v3"
    if site == "create":
        args = {**base, "record_id": "new", "record_class": "belief", "domain": "fact", "title": "new"}
        call = "create_current"
    elif site == "revise":
        args = {**base, "record_id": "phase:base", "operation_type": "refine", "changes": {"summary": "revised"}}
        call = "revise"
    elif site == "invalidate":
        args = {**base, "record_id": "phase:base"}
        call = "invalidate"
    elif site == "maintenance":
        args = {"actor": "synthetic", "reason": "R3 same-key", "run_id": "same-key", "idempotency_key": "same-key", "adjustments": [{"record_id": "phase:base", "field": "accessibility", "new_value": 0.8}]}
        call = "maintenance"
    elif site in {"add-relation", "retract-relation"}:
        args = {"relation_id": "same-key-relation", "from_record_id": "phase:base", "to_record_id": "core", "relation_type": "supports", "actor": "synthetic", "reason": "R3 same-key", "idempotency_key": "same-key"}
        if site == "retract-relation":
            prior = {**args, "idempotency_key": "same-key:assert"}
            assert run_case("py", root, {"surface": "kernel", "call": "add_relation", "arguments": prior})["stdout"].startswith(b"{")
        if site == "retract-relation":
            args.pop("relation_id")
        call = "add_relation" if site == "add-relation" else "retract_relation"
        table = "memory_relation_events_v4"
    elif site in {"core-proposal", "core-apply"}:
        args = {"reason": "R3 same-key", "phase_context": {"model": "synthetic"}}
        call = "identity_core_propose"
        table = "memory_core_proposals_v5"
        if site == "core-apply":
            proposal = json.loads(run_case("py", root, {"surface": "kernel", "call": call, "arguments": args})["stdout"])
            receipt = json.loads(run_case("py", root, {"surface": "kernel", "call": "owner_approve_core", "arguments": {"proposal_id": proposal["proposal_id"], "outcome": "apply"}})["stdout"])
            call, args, table = "identity_core_apply", {"receipt_id": receipt["receipt_id"]}, "memory_receipt_consumptions_v5"
    else:
        receipt = json.loads(run_case("py", root, {"surface": "kernel", "call": "owner_approve_retract", "arguments": {"record_id": "phase:base", "reason": "R3 same-key"}})["stdout"])
        call, args, table = "identity_retract", {"receipt_id": receipt["receipt_id"]}, "memory_receipt_consumptions_v5"
    return {"surface": "kernel", "call": call, "arguments": args}, table


def excluded(root, holder, victim, site):
    case, table = fixture_case(root, site)
    baseline = root / "serial"
    isolated_env(baseline)
    shutil.copy2(root / "store.sqlite3", baseline / "store.sqlite3")
    holder_case = {**case, "clock": "2026-10-01T00:00:00+00:00"}
    victim_case = {**case, "clock": "2026-10-02T00:00:00+00:00"}
    expected = [run_case("py", baseline, action) for action in (holder_case, victim_case)]
    assert all(o["exit"] == 0 and o["stdout"].startswith(b"{") for o in expected), expected
    children = []
    try:
        first, first_seen = start(holder, root, holder_case, label="holder", same_key_gate_role="holder", same_key_gate_table=table)
        children.append(first)
        assert first_seen.get(timeout=15) == b"R3-P18-INSERTED\n"
        second, second_seen = start(victim, root, victim_case, label="victim", same_key_gate_role="victim", same_key_gate_table=table)
        children.append(second)
        assert second_seen.get(timeout=15) == b"R3-P18-ATTEMPT\n"
        with_assertion = False
        try:
            marker = second_seen.get(timeout=0.1)
            raise AssertionError(("loser acquired before holder release", marker))
        except queue.Empty:
            with_assertion = True
        assert with_assertion and second.poll() is None
        one = finish(first)
        assert second_seen.get(timeout=15) == b"R3-P18-ACQUIRED\n"
        two = finish(second)
        assert [output(one), output(two)] == expected, (site, one, two, expected)
        for result in (one, two):
            checks = [u for u in result["units"] if u["call"] == "p18_precheck"]
            assert checks and all(u["immediate"] and u["table"] == table for u in checks), result
        assert schema_and_dump(root / "store.sqlite3") == schema_and_dump(baseline / "store.sqlite3")
        ready("py", root)
        ready("ts", root)
        assert not any((root / ("store.sqlite3" + s)).exists() for s in ("-wal", "-shm", "-journal"))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=20)
