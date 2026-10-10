"""P17 public calls with deterministic parent-controlled allocation ordering."""
import base64
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import isolated_env, schema_and_dump
from tools.r3.crashes import ready
from tools.r3.plans import step
from tools.r3.run import case_for_step, command, run_case, validate_case


def start(runtime, root, case, role):
    env = isolated_env(root)
    gate = root / (role + ".release")
    case = {**case, "root": str(root), "env": env, "allocation_role": role, "allocation_release": str(gate)}
    validate_case(root, case)
    process = subprocess.Popen(command(runtime), cwd=root, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen = queue.Queue()
    threading.Thread(target=lambda: seen.put(process.stderr.readline()), daemon=True).start()
    process.stdin.write(json.dumps(case).encode() + b"\n")
    process.stdin.flush()
    process.release_gate = gate
    return process, seen


def finish(process):
    process.release_gate.write_bytes(b"R")
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0 and not stderr, stderr
    result = json.loads(stdout)
    decoded = {"exit": result["exit"], "stdout": base64.b64decode(result["stdout"]),
               "stderr": base64.b64decode(result["stderr"])}
    return result, decoded


def run(root, holder, victim, operation):
    env = isolated_env(root)
    database = root / "store.sqlite3"
    shutil.copy2(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", database)
    seed = case_for_step(root, step("py", "mcp", "log-phase", event_id="race", title="Synthetic race",
                                  summary="synthetic", open_loop=True), 0, {})
    assert run_case("py", root, seed)["exit"] == 0
    baseline = root / "baseline"
    isolated_env(baseline)
    shutil.copy2(database, baseline / "store.sqlite3")
    def case(where, index):
        if operation == "close-loop":
            return case_for_step(where, step("py", "mcp", "close-loop", record_id="phase:race",
                                            note="synthetic close"), index, {})
        return {"surface": "kernel", "call": "revise", "clock": f"2026-10-0{index}T00:00:00+00:00",
                "arguments": {"record_id": "phase:race", "operation_type": "refine", "actor": "synthetic",
                              "reason": "synthetic revision", "evidence": {"source_ref": "synthetic:allocation"},
                              "idempotency_key": f"synthetic:allocation:{index}", "changes": {"title": f"Revision {index}"}}}
    expected = [run_case("py", baseline, case(baseline, i)) for i in (1, 2)]
    processes = []
    try:
        first, first_marker = start(holder, root, case(root, 1), "holder")
        processes.append(first)
        assert first_marker.get(timeout=15) == b"R3-ALLOCATION-HOLDER\n"
        second, second_marker = start(victim, root, case(root, 2), "victim")
        processes.append(second)
        assert second_marker.get(timeout=15) == b"R3-ALLOCATION-VICTIM\n"
        first_result, first_output = finish(first)
        second_result, second_output = finish(second)
        assert [first_output, second_output] == expected, (first_output, second_output, expected)
        for result in (first_result, second_result):
            sql = [u["sql"] for u in result["units"] if u["call"] == "sql_trace"]
            if sql:
                deciding = "FROM memory_current_v3" if operation == "revise" else "ORDER BY sequence_number DESC"
                read = next(i for i, statement in enumerate(sql) if deciding in statement)
                assert "BEGIN IMMEDIATE" in sql[:read], sql
        assert schema_and_dump(database) == schema_and_dump(baseline / "store.sqlite3")
        ready("py", root)
        ready("ts", root)
        assert not any((root / ("store.sqlite3" + suffix)).exists() for suffix in ("-wal", "-shm", "-journal"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=15)
