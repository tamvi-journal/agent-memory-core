"""R3 synthetic process adapters and sequential X runner."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import (assert_decay_margin, compare_trees, contained, expected_for_run,
                             isolated_env, snapshot)
from tools.r3.plans import fixed_plans, seeded_plan, step


def validate_case(root, case):
    contained(root, root / case.get("database", "store.sqlite3"))
    for index, item in enumerate(case.get("argv", [])):
        if item in ("--db", "--backup", "--out"):
            contained(root, root / Path(case["argv"][index + 1]))
        if item == "migrate-to":
            contained(root, root / Path(case["argv"][index + 1]))
        if item in ("--profile", "-p"):
            profile = case["argv"][index + 1]
            if profile != "example":
                contained(root, root / Path(profile))
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA",
                "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES", "TMPDIR", "TMP", "TEMP"):
        contained(root, Path(case["env"][key]))


def command(runtime):
    if runtime == "py":
        return [sys.executable, "-B", str(ROOT / "tools/r3/worker.py")]
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node 22 is required for R3")
    return [node, "--experimental-strip-types", str(ROOT / "tools/r3/worker.ts")]


def run_case(runtime, root, case):
    root = root.resolve()
    env = isolated_env(root)
    case = {**case, "root": str(root), "env": env}
    validate_case(root, case)
    process = subprocess.run(command(runtime), input=json.dumps(case, ensure_ascii=True).encode() + b"\n",
                             cwd=ROOT, env=env, capture_output=True, timeout=40)
    if process.returncode:
        raise AssertionError(f"{runtime} harness failed: {process.stderr.decode(errors='replace')}")
    result = json.loads(process.stdout)
    assert not process.stderr, process.stderr.decode(errors="replace")
    return {**({"units": result["units"]} if case.get("capture") else {}), "exit": result["exit"], "stdout": base64.b64decode(result["stdout"]),
            "stderr": base64.b64decode(result["stderr"])}


def resolve_refs(value, saved):
    if isinstance(value, dict) and set(value) == {"$ref"}:
        current = saved
        for key in value["$ref"].split("."):
            current = current[key]
        return current
    if isinstance(value, dict):
        return {key: resolve_refs(item, saved) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, saved) for item in value]
    return value


def case_for_step(root, action, index, saved):
    args = resolve_refs(action.get("arguments", {}), saved)
    operation = action["operation"]
    clock = (datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(days=index)).isoformat()
    case = {"surface": action["surface"], "clock": clock, "database": action.get("database", "store.sqlite3")}
    if action["surface"] == "mcp":
        name = "identity_" + operation.replace("-", "_")
        if operation == "apply-receipt":
            purpose = args.pop("purpose", "apply")
            name = {"apply": "identity_core_apply", "reject": "identity_core_apply",
                    "retract": "identity_retract", "legacy-close": "identity_close_legacy_discussion"}[purpose]
        frame = json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                            "params": {"name": name, "arguments": args}}, ensure_ascii=True).encode() + b"\n"
        case["chunks"] = [base64.b64encode(frame).decode()]
    else:
        argv = ["--profile", "example", "--db", str(contained(root, root / case["database"])), operation]
        if operation in {"log-phase", "log-fact"}:
            argv += [args.pop("event_id" if operation == "log-phase" else "fact_id")]
            for key in ("title", "summary", "content", "source"):
                if key in args:
                    argv += ["--" + key, args[key]]
            if args.get("open_loop"):
                argv.append("--open-loop")
        elif operation == "retrieve":
            argv.append(args["cue"])
            if not args.get("track", True): argv.append("--readonly")
            if args.get("packet"): argv.append("--packet")
        elif operation == "close-loop": argv += [args["record_id"], "--note", args["note"]]
        elif operation == "apply-receipt": argv.append(args["receipt_id"])
        elif operation == "approve-core":
            argv += [args["proposal_id"], "--" + args["outcome"], "--issue-only"]
            case.update(tty=True, stdin=base64.b64encode((args["outcome"].upper() + " " + args["proposal_id"].split(":", 1)[1][:12] + "\n").encode()).decode())
        elif operation == "approve-retract":
            argv += [args["record_id"], "--reason", args["reason"], "--issue-only"]
            case.update(tty=True, stdin=base64.b64encode(("RETRACT " + args["record_id"] + "\n").encode()).decode())
        elif operation == "close-legacy-discussion":
            argv += ["--note", args["note"], "--issue-only"]
            uri = (root / case["database"]).as_uri() + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as conn:
                event = conn.execute("SELECT relation_event_id FROM memory_relation_events_v4 WHERE relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1").fetchone()[0]
            case.update(tty=True, stdin=base64.b64encode(("CLOSE " + event.split(":", 1)[1][:12] + "\n").encode()).decode())
        elif operation == "migrate-to":
            argv.append(str(contained(root, root / args["target"])))
            if args.get("dry_run"): argv.append("--dry-run")
        case["argv"] = argv
    return case


def receipt_steps(plan):
    issuer, consumer, kind = plan["issuer"], plan["consumer"], plan["receipt"]
    actions = [] if kind == "legacy-close" else [step(issuer, "cli", "init")]
    if kind in {"apply", "reject"}:
        propose = step(issuer, "mcp", "core-propose", reason="synthetic cross-runtime proposal", phase_context={"model": "r3", "2": 1, "1": 1.0})
        propose["save"] = "proposal"
        actions.append(propose)
        issue = step(issuer, "cli", "approve-core", proposal_id={"$ref": "proposal.proposal_id"}, outcome=kind)
    elif kind == "retract":
        actions.append(step(issuer, "mcp", "log-phase", event_id="retract", title="Retract cue", summary="synthetic"))
        issue = step(issuer, "cli", "approve-retract", record_id="phase:retract", reason="synthetic retract")
    else:
        issue = step(issuer, "cli", "close-legacy-discussion", note="synthetic close")
    issue["save"] = "receipt"
    actions += [issue, step(consumer, "mcp", "apply-receipt", receipt_id={"$ref": "receipt.receipt_id"}, purpose=kind),
                step(consumer, "cli", "doctor"), step(issuer, "mcp", "status")]
    return actions


def read_result(case, output):
    raw = output["stdout"]
    if case["surface"] == "mcp":
        response = json.loads(raw.splitlines()[-1])
        return response["result"]["structuredContent"]
    return json.loads(raw[raw.index(b"{"):])


def run_plan(plan, parent):
    root = parent / plan["name"]
    oracle, actual = root / "py", root / "mixed"
    isolated_env(oracle); isolated_env(actual)
    if plan.get("receipt") == "legacy-close":
        fixture = ROOT / "spec/golden-cli-v1/authority-legacy-close-issue-only/fixture/store.sqlite3"
        for target in (oracle, actual): shutil.copy2(fixture, target / "store.sqlite3")
    if plan.get("fixture"):
        for target in (oracle, actual): shutil.copy2(ROOT / plan["fixture"], target / "store.sqlite3")
    actions = receipt_steps(plan) if "receipt" in plan else plan["steps"]
    saved_a, saved_b = {}, {}
    decay = False
    for index, action in enumerate(actions):
        case_a = case_for_step(oracle, action, index, saved_a)
        case_b = case_for_step(actual, action, index, saved_b)
        if action["operation"] == "decay":
            # Deterministically regenerate the moment if a generated margin is too small.
            for offset in range(100):
                moment = (datetime.fromisoformat(case_a["clock"]) + timedelta(seconds=offset)).isoformat()
                try: assert_decay_margin(oracle / "store.sqlite3", moment)
                except ValueError: continue
                case_a["clock"] = case_b["clock"] = moment
                break
            else: raise AssertionError("no safe generated decay margin")
        before_a, before_b = snapshot(oracle), snapshot(actual)
        a = run_case("py", oracle, case_a)
        b = run_case(action["runtime"], actual, case_b)
        assert a["exit"] == b["exit"], (plan["name"], index, a, b)
        for file in ("stdout", "stderr"):
            context = "raw-text" if file == "stderr" or case_a.get("tty") else "json-string"
            assert expected_for_run(a[file], file, oracle, actual, context) == b[file], (plan["name"], index, file, a[file], b[file])
        decay |= action["operation"] == "decay"
        compare_trees(oracle, actual, before_a, before_b, decay=decay)
        if "save" in action:
            saved_a[action["save"]] = read_result(case_a, a)
            saved_b[action["save"]] = read_result(case_b, b)
    return {"plan": plan["name"], "steps": len(actions)}


def sequential(parent, *, seeds=200):
    plans = fixed_plans()
    seed_list = json.loads((ROOT / "tools/r3/seeds.json").read_text())
    assert seeds == 200, "CI budget is fixed; use a single named plan for debugging"
    plans += [seeded_plan(seed) for seed in seed_list]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda p: run_plan(p, parent), plans))
    return {"matrix": len(fixed_plans()), "seeded": seeds, "steps": sum(r["steps"] for r in results)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="r3-x-") as folder:
        parent = Path(folder).resolve()
        if args.plan:
            plans = fixed_plans() + [seeded_plan(seed) for seed in json.loads((ROOT / "tools/r3/seeds.json").read_text())]
            selected = next(plan for plan in plans if plan["name"] == args.plan)
            print(json.dumps(run_plan(selected, parent)))
        else:
            print(json.dumps(sequential(parent)))
