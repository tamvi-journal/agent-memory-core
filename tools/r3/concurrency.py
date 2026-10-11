"""Cross-runtime concurrency with Python replay of every small serial history."""
import base64
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import isolated_env, schema_and_dump
from tools.r3.crashes import ready
from tools.r3.crashes import intake_case
from tools.r3.plans import candidate_orders
from tools.r3.replay import replay_units
from tools.r3.run import command, run_case, validate_case, case_for_step
from tools.r3.plans import step


def program(kind, runtime, count, seed, root):
    cases = []
    for index in range(count):
        if kind == "revise":
            case = {"surface": "kernel", "call": "revise", "arguments": {
                "record_id": "phase:base", "operation_type": "refine", "actor": "synthetic",
                "reason": "R3 concurrency", "evidence": {"source_ref": f"synthetic:{seed}:{runtime}:{index}"},
                "idempotency_key": f"concurrent:{seed}:{runtime}:{index}",
                "changes": {"summary": f"{seed}:{runtime}:{index}"},
            }}
        elif kind.startswith("g2-"):
            updates = {"record_id": f"g2-{runtime}-{seed}", "idempotency_key": f"g2:{runtime}:{seed}"}
            if kind == "g2-held": updates["unresolved_conflict"] = True
            else: updates.update(operation_type="refine", record_id="phase:base", changes={})
            case = intake_case(**updates)
        elif kind == "g10":
            case = case_for_step(root, step(runtime, "mcp", "log-phase", event_id=runtime + "-concurrent",
                                 title=runtime + " concurrent", summary="synthetic"), index, {})
        else:
            case = case_for_step(root, step(runtime, "mcp", "retrieve", cue="unique concurrency cue", track=True, limit=1), index, {})
        case.update(capture=True, clock=f"2026-10-{index+1:02}T{0 if runtime == 'py' else 12:02}:00:00+00:00")
        cases.append(case)
    return cases


def run_process(runtime, root, cases, barrier):
    env = isolated_env(root)
    child = subprocess.Popen(command(runtime), cwd=root, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    history = []
    try:
        for index, case in enumerate(cases):
            case = {**case, "root": str(root), "env": env}
            validate_case(root, case)
            barrier.wait(timeout=20)
            invoked = time.monotonic_ns()
            child.stdin.write(json.dumps(case).encode() + b"\n")
            child.stdin.flush()
            response = child.stdout.readline()
            returned = time.monotonic_ns()
            assert response, (runtime, child.stderr.read())
            result = json.loads(response)
            assert result["exit"] == 0, result
            wire = base64.b64decode(result["stdout"])
            assert not base64.b64decode(result["stderr"]), result
            if case["surface"] == "mcp":
                outcome = json.loads(wire)["result"]
                if outcome.get("isError"):
                    assert outcome["content"][0]["text"] == "StoreBusy: store is busy; retry later", outcome
            elif wire.startswith(b"StoreBusy:"):
                assert wire == b"StoreBusy: store is busy; retry later"
            else:
                assert wire.startswith(b"{"), wire
            history.append({"runtime": runtime, "index": index, "invoke_ts": invoked,
                            "return_ts": returned, "units": result["units"]})
        child.stdin.close()
        child.stdin = None
        out, err = child.communicate(timeout=20)
        assert child.returncode == 0 and not out and not err, (child.returncode, out, err)
        return history
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=20)


def one(parent, fixture, seed, kind, count):
    root = parent / f"{seed}-{kind}-{count}"
    isolated_env(root)
    shutil.copy2(fixture, root / "store.sqlite3")
    barrier = threading.Barrier(2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_process, runtime, root, program(kind, runtime, count, seed, root), barrier)
                   for runtime in ("py", "ts")]
        histories = [future.result(timeout=90) for future in futures]
    units, sequences, edges = {}, [], set()
    for process in histories:
        sequence = []
        for op in process:
            op["unit_ids"] = []
            for index, unit in enumerate(op["units"]):
                name = f"{op['runtime']}:{op['index']}:{index}"
                units[name] = unit
                sequence.append(name)
                op["unit_ids"].append(name)
        sequences.append(sequence)
    operations = [op for process in histories for op in process]
    for a in operations:
        for b in operations:
            if a["return_ts"] < b["invoke_ts"]:
                edges.update((x, y) for x in a["unit_ids"] for y in b["unit_ids"])
    schema, actual = schema_and_dump(root / "store.sqlite3")
    if count > 3:
        # Store-local evidence constrains the search; return-log order never does.
        by_key = {unit["arguments"].get("idempotency_key"): name for name, unit in units.items()}
        records = {}
        for row in actual["tables"]["memory_revisions_v3"]:
            name = by_key.get(row["idempotency_key"])
            if name:
                records.setdefault(row["record_id"], []).append((row["revision_number"], name))
        for rows in records.values():
            ordered = [name for _, name in sorted(rows)]
            edges.update(zip(ordered, ordered[1:]))
    candidates, witnesses, refused = 0, [], 0
    for order in candidate_orders(sequences, before=edges):
        candidates += 1
        try:
            path = replay_units(fixture, root / f"candidate-{candidates}", [units[name] for name in order])
        except ValueError:
            # A stale old_value or another ordinary serial refusal rejects this order.
            # It does not establish a witness, and every remaining small order is still replayed.
            refused += 1
            continue
        expected_schema, expected = schema_and_dump(path)
        if expected_schema == schema and expected == actual:
            witnesses.append(order)
            if count > 3:
                break
    assert witnesses, {"seed": seed, "kind": kind, "count": count, "candidates": candidates}
    ready("py", root)
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (root / ("store.sqlite3" + suffix)).exists()
    assert not (root / "store.activation.json").exists()
    (root / "history.json").write_text(json.dumps({"histories": histories, "witness": witnesses[0]}, indent=2))
    return {"seed": seed, "kind": kind, "operations_per_process": count,
            "transactions": len(units), "replayed_candidates": candidates, "refused_candidates": refused,
            "witnesses": len(witnesses)}


def run(parent):
    fixture_root = parent / "fixture"
    isolated_env(fixture_root)
    shutil.copyfile(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", fixture_root / "store.sqlite3")
    run_case("py", fixture_root, {"surface": "kernel", "call": "log_phase", "arguments": {
        "event_id": "base", "title": "Unique concurrency cue", "summary": "synthetic", "cues": ["unique concurrency cue"],
    }})
    seeds = json.loads((ROOT / "tools/r3/seeds.json").read_text())[:3]
    results = [one(parent, fixture_root / "store.sqlite3", seed, kind, count)
               for seed in seeds for kind, count in (("revise", 3), ("revise", 4), ("g10", 1), ("g12", 1),
                                                     ("g2-held", 1), ("g2-no-op", 1))]
    from tools.r3.idempotency import intake
    shared = []
    for seed in seeds:
        for variant in ("same", "different", "received"):
            dumps = []
            for loser, winner in (("py", "ts"), ("ts", "py")):
                dump, proof = intake(parent / f"shared-{seed}-{variant}-{loser}", loser, winner, variant, seed)
                dumps.append(dump)
                shared.append({**proof, "kind": "g2-shared-key", "operations_per_process": 1,
                               "replayed_candidates": 1, "witnesses": 1})
            assert dumps[0] == dumps[1]
    return {"runs": len(results) + len(shared), "seeds": seeds, "results": results,
            "shared_key_runs": len(shared), "shared_key_results": shared}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="r3-concurrent-") as folder:
        print(json.dumps(run(Path(folder).resolve())))
