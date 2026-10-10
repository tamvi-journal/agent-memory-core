"""Pure bounded differential inputs; expected outcomes always come from Python."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import queue
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import isolated_env, snapshot, compare_trees, expected_for_run, assert_decay_margin
from tools.r3.run import command, validate_case
from tools.golden_cli.generate import usage_prefix
from tools.golden_cli.tokens import render

BUDGETS = {"mcp": 2000, "cli": 2000, "kernel": 5000}
CAPS = {"mcp_bytes": 65536, "nesting": 32, "mcp_frames": 16, "chunks": 16,
        "cli_bytes": 8192, "kernel_bytes": 65536, "evidence_items": 32}
FIELDS = ("evidence_type", "source_ref", "source_family", "independence_group", "captured_at", "actor",
          "surface", "model_family", "content_summary", "privacy_class", "identity_version")


def number(seed, index):
    return int.from_bytes(hashlib.sha256(f"r3-z-v1:{seed}:{index}".encode()).digest()[:8], "big")


def partition(raw, value):
    count = min(16, max(1, len(raw)), 1 + value % 16)
    cuts = sorted({0, len(raw), *(number(value, i) % (len(raw) + 1) for i in range(count - 1))})
    return [base64.b64encode(raw[a:b]).decode() for a, b in zip(cuts, cuts[1:])] or [""]


def generate(boundary, seed, index):
    case = _generate(boundary, seed, index)
    if boundary == "cli":
        assert sum(len(token.encode()) for token in case["argv"]) + len(base64.b64decode(case.get("stdin", ""))) <= CAPS["cli_bytes"]
    return case


def _generate(boundary, seed, index):
    value = number(seed, index)
    if boundary == "mcp":
        paths = sorted((ROOT / "spec/golden-mcp-v1").glob("*/transcript.in"))
        paths = [p for p in paths if p.stat().st_size <= 65536 and frame_count(p.read_bytes()) <= 16]
        raw = paths[value % len(paths)].read_bytes()
        mode = index % 8
        if mode == 0 and raw:
            position = number(value, 1) % len(raw)
            raw = raw[:position] + bytes([number(value, 2) % 256]) + raw[position + 1:]
        elif mode == 1 and raw: raw = raw[:number(value, 3) % len(raw)]
        elif mode == 2: raw = raw.replace(b"\n", b"\r\n")
        elif mode == 3: raw = raw.rstrip(b"\n")
        elif mode == 4:
            identifiers = [None, False, [], {}, 1.0, 9007199254740991, "\U0001f9e0", ""]
            frame = {"jsonrpc": "2.0", "id": identifiers[value % len(identifiers)], "method": "tools/call",
                     "params": {"name": "identity_retrieve", "arguments": {"cue": "synthetic", "track": False,
                       "limit": [0, 1, 100, 101, None, "1", True][number(value, 4) % 7]}}}
            raw = json.dumps(frame, ensure_ascii=True).encode() + b"\n"
        elif mode == 5:
            nested = "synthetic"
            for _ in range(value % 25): nested = [nested]
            raw = json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                "name": "identity_log_phase", "arguments": {"event_id": "fuzz", "title": "synthetic",
                 "summary": "synthetic", "unknown": nested}}}).encode() + b"\n"
        elif mode == 6: raw = b'\xff\n{"jsonrpc":"2.0","id":1,"method":"ping"}'
        assert len(raw) <= 65536 and frame_count(raw) <= 16 and nesting(raw) <= 32
        return {"surface": "mcp", "chunks": partition(raw, value)}
    if boundary == "cli":
        integers = ["0", "1", "200", "1000", "-1", "1.0", "+0002", "\u0661", "\u00a01\u00a0", "1_0", "1e2"]
        choice = index % 13
        if choice == 9:
            profile = json.loads((ROOT / "trajecta_identity/profiles/example/profile.json").read_text())
            field = ["name", "core", "v", "anchors"][value % 4]
            profile[field] = [None, False, 42, ["synthetic"], {"nested": "synthetic"}, "synthetic"][number(value, 6) % 6]
            raw = json.dumps(profile, ensure_ascii=True).encode()
            if value % 7 == 0: raw = raw[:number(value, 7) % len(raw)]
            if value % 11 == 0: raw = b"\xff"
            return {"surface": "cli", "argv": ["status"], "profile_bytes": base64.b64encode(raw).decode()}
        if choice == 10:
            options = [["--title", "synthetic"], ["--summary", "synthetic"], ["--content", "\U0001f9e0\u0301"]]
            options.sort(key=lambda pair: number(value, len(pair[0])))
            return {"surface": "cli", "argv": ["log-fact", "fuzz", *(token for pair in options for token in pair)]}
        if choice == 11:
            name = ["authority-core-apply-issue-only", "authority-retract-issue-only", "authority-legacy-close-issue-only"][value % 3]
            raw = [b"WRONG\n", b"\xff\n", b"APPLY \n", b"\r\n", b"", b"\xc2\xa0RETRACT phase:retract-me\n"][number(value, 8) % 6]
            return {"surface": "cli", "scenario": name, "argv": json.loads((ROOT / f"spec/golden-cli-v1/{name}/argv.json").read_text()),
                    "tty": True, "stdin": base64.b64encode(raw).decode()}
        if choice == 12:
            paths = sorted((ROOT / "spec/golden-cli-v1").glob("integer-*/argv.json"))
            return {"surface": "cli", "argv": json.loads(paths[value % len(paths)].read_text())}
        argv = [["status"], ["doctor"], ["timeline", "--limit", integers[value % len(integers)]],
                ["retrieve", "synthetic", "--readonly", "--limit", integers[value % len(integers)]],
                ["log-phase", "fuzz", "--title", "synthetic", "--summary", "synthetic", "--open-loop"],
                ["log-fact", "fuzz", "--title", "\U0001f9e0\u0301", "--summary", "synthetic"],
                ["close-loop", "phase:missing", "--note", "synthetic"],
                ["core-proposals"], ["decay"]][choice]
        if value % 5 == 0: argv += ["--unknown-r3"]
        return {"surface": "cli", "argv": argv}
    if boundary != "kernel": raise ValueError("unknown generator boundary")
    evidence = {"source_ref": "synthetic:r3-fuzz", "content_summary": "synthetic", "confidence": 0.9}
    invalid = [None, False, 42, 1.5, ["nested"], {"nested": "x" * (value % 12000)}]
    field = FIELDS[value % len(FIELDS)]
    if index % 3 != 0: evidence[field] = copy.deepcopy(invalid[number(value, 5) % len(invalid)])
    else: evidence[field] = "synthetic:" + "\U0001f9e0\u0301" * (value % 20)
    evidence["source_payload"] = {"2": index, "1": float(index), "nested": ["synthetic", {"text": "\u00e9\u0301"}]}
    proposal = {"operation_type": "create", "record_id": "synthetic-r3", "record_class": "event", "domain": "phase",
                "actor": "synthetic", "reason": "synthetic", "logic": "synthetic", "truth_basis": "synthetic",
                "changes": {"title": "Synthetic", "summary": "synthetic"}, "evidence": [evidence],
                "idempotency_key": "synthetic:r3"}
    if index % 17 == 0: proposal["record_id"] = "core"
    if index % 19 == 0: proposal["unresolved_conflict"] = True
    if index % 23 == 0:
        proposal["evidence"] = [{**evidence, "source_ref": f"synthetic:r3-fuzz:{i}"} for i in range(1 + value % 32)]
        if len(json.dumps(proposal).encode()) > 65536:
            proposal["evidence"] = proposal["evidence"][:1]
    assert len(json.dumps(proposal).encode()) <= 65536 and len(proposal["evidence"]) <= 32
    return {"surface": "kernel", "call": "runtime_submit", "arguments": {"proposal": proposal}}


def frame_count(raw):
    return raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))


def nesting(raw):
    """Lexical cap also covers malformed input; quoted brackets do not nest."""
    depth = maximum = 0
    quoted = escaped = False
    for byte in raw:
        if escaped: escaped = False; continue
        if quoted and byte == 92: escaped = True; continue
        if byte == 34: quoted = not quoted; continue
        if not quoted:
            if byte in (91, 123): depth += 1; maximum = max(maximum, depth)
            elif byte in (93, 125): depth = max(0, depth - 1)
    return maximum


def shrink(case, fails):
    """Deterministic deletion minimization; every deletion is checked against Python."""
    current = copy.deepcopy(case)
    if current["surface"] == "mcp":
        raw = b"".join(base64.b64decode(x) for x in current["chunks"])
        width = max(1, len(raw) // 2)
        while width:
            position = 0
            while position < len(raw):
                trial = raw[:position] + raw[position + width:]
                candidate = {**current, "chunks": [base64.b64encode(trial).decode()]}
                if fails(candidate): raw, current = trial, candidate
                else: position += width
            width = width // 2
    elif current["surface"] == "cli":
        for i in range(len(current["argv"]) - 1, -1, -1):
            trial = {**current, "argv": current["argv"][:i] + current["argv"][i + 1:]}
            if fails(trial): current = trial
    else:
        for key in sorted(current["arguments"]["proposal"]):
            trial = copy.deepcopy(current)
            del trial["arguments"]["proposal"][key]
            if fails(trial): current = trial
    return current


class Worker:
    def __init__(self, runtime, root):
        self.child = subprocess.Popen(command(runtime), cwd=root, env=isolated_env(root), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.responses = queue.Queue()
        self.errors = bytearray()
        self.reader = threading.Thread(target=lambda: [self.responses.put(line) for line in self.child.stdout], daemon=True)
        self.error_reader = threading.Thread(target=lambda: self.errors.extend(self.child.stderr.read()), daemon=True)
        self.reader.start(); self.error_reader.start()
    def call(self, root, case):
        env = isolated_env(root)
        case = {**case, "root": str(root), "env": env}
        if case["surface"] == "cli" and not case.get("scenario"):
            case["argv"] = ["--profile", "example", "--db", str(root / "store.sqlite3"), *case["argv"]]
        validate_case(root, case)
        self.child.stdin.write(json.dumps(case).encode() + b"\n")
        self.child.stdin.flush()
        try: raw = self.responses.get(timeout=40)
        except queue.Empty: raise RuntimeError("worker did not finish its bounded case")
        if not raw: raise RuntimeError("worker exited before returning a case")
        result = json.loads(raw)
        return {"exit": result["exit"], "stdout": base64.b64decode(result["stdout"]), "stderr": base64.b64decode(result["stderr"])}
    def close(self):
        if self.child.poll() is None:
            self.child.stdin.close(); self.child.stdin = None
            self.child.wait(timeout=20)
            self.reader.join(); self.error_reader.join()
            assert self.child.returncode == 0 and self.responses.empty() and not self.errors, bytes(self.errors)
    def abort(self):
        if self.child.poll() is None: self.child.kill(); self.child.wait(timeout=20)
        self.reader.join(timeout=20); self.error_reader.join(timeout=20)
        self.child.stdout.close(); self.child.stderr.close()
        if self.child.stdin and not self.child.stdin.closed: self.child.stdin.close()


def render_scenario_argv(argv, registry, root):
    return json.loads(render(json.dumps(argv).encode(), registry, file="argv.json", root=root, origin=""))


def compare(parent, workers, case):
    roots = [parent / runtime for runtime in ("py", "ts")]
    before, output = [], []
    for root, worker in zip(roots, workers):
        isolated_env(root)
        actual_case = copy.deepcopy(case)
        if case.get("scenario"):
            scenario = ROOT / "spec/golden-cli-v1" / case["scenario"]
            shutil.copytree(scenario / "fixture", root, dirs_exist_ok=True)
            registry = json.loads((scenario / "tokens.json").read_text())
            actual_case["argv"] = render_scenario_argv(case["argv"], registry, str(root))
        else:
            shutil.copy2(ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3", root / "store.sqlite3")
        if case.get("profile_bytes"):
            (root / "profiles/example/profile.json").write_bytes(base64.b64decode(case["profile_bytes"]))
        decay = contains_decay(case)
        if decay: assert_decay_margin(root / "store.sqlite3", "2026-09-30T00:00:00+00:00")
        before.append(snapshot(root))
        output.append(worker.call(root, actual_case))
    a, b = output
    assert a["exit"] == b["exit"], "exit code"
    if case["surface"] == "cli" and a["exit"] == 2:
        assert not a["stdout"] and not b["stdout"]
        assert usage_prefix(a["stderr"]) == usage_prefix(b["stderr"]), "raising parser prefix"
    else:
        assert expected_for_run(a["stdout"], "stdout", *roots) == b["stdout"], "stdout"
        if case["surface"] == "cli" and a["exit"] == 1 and b"Traceback (most recent call last)" in a["stderr"]:
            assert a["stderr"] and b["stderr"], "A13 requires nonempty untyped diagnostics"
        elif case["surface"] != "mcp":
            assert expected_for_run(a["stderr"], "stderr", *roots, "raw-text") == b["stderr"], "stderr"
    compare_trees(*roots, *before, decay=decay)


def contains_decay(case):
    if case["surface"] == "cli": return "decay" in case["argv"]
    if case["surface"] == "mcp":
        raw = b"".join(base64.b64decode(c) for c in case["chunks"])
        return b"identity_decay" in raw
    return case.get("call") == "decay"


def run(parent, budgets, seed):
    workers = [Worker(runtime, parent / (runtime + "-worker")) for runtime in ("py", "ts")]
    failures, counts = [], {}
    try:
        for boundary, count in budgets.items():
            counts[boundary] = 0
            for index in range(count):
                case = generate(boundary, seed, index)
                with tempfile.TemporaryDirectory(prefix="z-case-", dir=parent) as folder:
                    try: compare(Path(folder), workers, case)
                    except AssertionError as error:
                        failures.append({"boundary": boundary, "seed": seed, "index": index, "reason": str(error), "case": case})
                counts[boundary] += 1
            print(json.dumps({"completed": boundary, "count": counts[boundary], "seed": seed}), flush=True)
        for worker in workers: worker.close()
    finally:
        for worker in workers: worker.abort()
    return {"counts": counts, "seed": seed, "caps": CAPS, "failures": failures}


def minimize_failures(parent, result):
    """Persist a checked minimal representative per mismatch, never auto-commit."""
    workers = [Worker(runtime, parent / (runtime + "-shrink")) for runtime in ("py", "ts")]
    seen = set()
    try:
        for failure in result["failures"]:
            key = (failure["boundary"], failure["reason"])
            if key in seen: continue
            seen.add(key)
            def fails(case):
                with tempfile.TemporaryDirectory(prefix="z-shrink-", dir=parent) as folder:
                    try: compare(Path(folder), workers, case)
                    except AssertionError as error: return str(error) == failure["reason"]
                return False
            assert fails(failure["case"]), "mismatch did not reproduce"
            failure["minimal_case"] = shrink(failure["case"], fails)
            assert fails(failure["minimal_case"])
        for worker in workers: worker.close()
    finally:
        for worker in workers: worker.abort()


def transport_smokes(parent):
    frame = b'{"jsonrpc":"2.0","id":"\xf0\x9f\xa7\xa0","method":"ping"}'
    plans = [[bytes([byte]) for byte in frame + b"\n"], [frame[:17], frame[17:]], [b"\xff\n", frame]]
    for index, chunks in enumerate(plans):
        results = []
        for runtime in ("py", "ts"):
            root = parent / f"transport-{index}-{runtime}"
            env = isolated_env(root); before = snapshot(root)
            cmd = ([sys.executable, "-B", "-m", "trajecta_identity.mcp_server"] if runtime == "py" else
                   [shutil.which("node"), "--experimental-strip-types", str(ROOT / "node/src/mcp.ts")])
            child = subprocess.Popen(cmd + ["--profile", "example", "--db", "store.sqlite3"],
                                     cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for chunk in chunks: child.stdin.write(chunk); child.stdin.flush()
                child.stdin.close(); child.stdin = None
                out, err = child.communicate(timeout=20)
                # R3's transport law pins stdout/exit only; G7 diagnostics stay frozen.
                assert child.returncode == 0
                results.append(out)
                assert snapshot(root) == before
            finally:
                if child.poll() is None: child.kill(); child.communicate(timeout=20)
        assert results[0] == results[1] and results[0]
    return len(plans)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nightly", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    budgets = {k: v * (4 if args.nightly else 1) for k, v in BUDGETS.items()}
    seed = number(0, int(datetime.now(timezone.utc).strftime("%Y%m%d"))) if args.nightly else 451714940
    with tempfile.TemporaryDirectory(prefix="r3-z-") as folder:
        parent = Path(folder).resolve()
        result = run(parent, budgets, seed)
        if result["failures"]: minimize_failures(parent, result)
        result["transport_smokes"] = transport_smokes(parent)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "failures"}))
    raise SystemExit(bool(result["failures"]))
