"""Generate the R2d CLI oracle under Python 3.11 / Unicode 14 in the pinned image.

All child roots and environments are isolated before the first process is spawned.
The injected TTY and clocks are test harnesses; production has no bypass flag.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from memory_core import MemoryStore
from tools.golden.generate import clock_context, dump_database
from tools.golden_mcp import generate as mcp
from tools.golden_cli.tokens import template, render
from tools.golden_cli.decode import differential_table

ORACLE_COMMIT = "4cbc3627f9edbe3a013fc1ea17d8188c5df47dd9"  # main + the committed P10/P13/P14 oracle patches
ORACLE_SEMANTICS = [*mcp.ORACLE_SEMANTICS, "P10-read-only-doctor", "P11-read-no-create",
                    "P12-successful-resolve-only", "P13-public-cli-errors", "P14-backup-alias"]
BUNDLED = ROOT / "trajecta_identity/profiles"


def sha(raw): return hashlib.sha256(raw).hexdigest()


def usage_prefix(stderr: bytes) -> bytes:
    """R3 §3.1b: observe the raising parser, independently validate its prog."""
    from trajecta_identity.cli import parser
    root = parser()
    progs = {root.prog}
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            progs.update(command.prog for command in action.choices.values())
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    assert lines, "usage error has no stderr line"
    prog, marker, _ = lines[-1].partition(b": error:")
    assert marker and prog.decode("utf-8") in progs, "unknown raising parser prog"
    return prog + marker


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())


def ranges(items):
    result = []
    for cp in items:
        if result and result[-1][1] + 1 == cp: result[-1][1] = cp
        else: result.append([cp, cp])
    return result


def table_values():
    assigned = [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]
    decimal = []
    for cp in assigned:
        value = unicodedata.decimal(chr(cp), None)
        if value is None: continue
        if decimal and decimal[-1][1]+1 == cp and value == (cp-decimal[-1][0]+decimal[-1][2]):
            decimal[-1][1] = cp
        else: decimal.append([cp, cp, value])
    strip = [cp for cp in assigned if chr(cp).isspace()]
    integers = []
    for cp in strip:
        try: int(chr(cp) + "1" + chr(cp))
        except ValueError: continue
        integers.append(cp)
    return {"isalnum": {"ranges": ranges(cp for cp in assigned if chr(cp).isalnum())},
            "decimal": {"ranges": decimal},
            "whitespace": {"strip": strip, "integer": integers}}


def write_tables(output):
    hashes = {}
    for name, values in table_values().items():
        payload = {"schema": f"trajecta.cli-{name}/v1", "unicode": "14.0.0",
                   "generator": "tools/golden_cli/generate.py", "oracle_commit": ORACLE_COMMIT, **values}
        payload["sha256"] = sha(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
        path = output / (name + ".json")
        write_json(path, payload)
        hashes[name] = sha(path.read_bytes())
    return hashes


def isolated_env(work, clock):
    env = {"PATH": os.defpath, "PYTHONPATH": os.pathsep.join((str(clock), str(ROOT))),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "0")}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ: env[key] = os.environ[key]
    for key, directory in {"HOME": "home", "USERPROFILE": "home", "XDG_DATA_HOME": "xdg",
        "LOCALAPPDATA": "local", "APPDATA": "app", "TRAJECTA_IDENTITY_DATA_DIR": "data",
        "TRAJECTA_IDENTITY_PROFILES": "profiles"}.items():
        env[key] = str(work / directory)
    return env


def schema_record(path):
    # immutable avoids journal handling while inspecting the oracle fixture.
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        return {"application_id": db.execute("PRAGMA application_id").fetchone()[0],
                "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                "sqlite_master": [list(row) for row in db.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]}


def database_classes(work, initial, argv):
    source = argv[argv.index("--db") + 1] if "--db" in argv else None
    paths = {p for p in initial if p.endswith(".sqlite3")}
    paths.update(p.relative_to(work).as_posix() for p in work.rglob("*.sqlite3"))
    if source: paths.add(source)
    backup = None
    if "migrate-to" in argv:
        target = argv[argv.index("migrate-to") + 1]; paths.add(target)
        if "--backup" in argv: backup = argv[argv.index("--backup") + 1]
        elif source and (work / source).exists():
            version = schema_record(work / source)["user_version"]
            if version in {2, 3, 4}: backup = target + f".v{version}.bak"
        if backup: paths.add(backup)
    classes = {}
    for relative in sorted(paths):
        path = work / relative
        if not path.exists(): classes[relative] = {"class": "absent"}; continue
        digest = sha(path.read_bytes())
        if initial.get(relative, {}).get("sha256") == digest:
            classes[relative] = {"class": "unchanged", "oracle_sha256": digest}
        elif relative == backup and source in initial:
            assert source and digest == initial[source]["sha256"]
            assert path.stat().st_mtime_ns == (work / source).stat().st_mtime_ns, (relative, path.stat().st_mtime_ns, (work / source).stat().st_mtime_ns)
            classes[relative] = {"class": "backup", "source": source, "oracle_sha256": digest}
        else:
            assert not any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
            classes[relative] = {"class": "written", "oracle_sha256": digest, "schema": schema_record(path)}
    return classes


def snapshot(work):
    return {p.relative_to(work).as_posix(): {"kind": "file", "sha256": sha(p.read_bytes())}
            if p.is_file() else {"kind": "directory"} for p in sorted(work.rglob("*"))}


def store_state(work, state):
    store = work / "store.sqlite3"
    if state == "missing": return
    if state == "uninitialized":
        with sqlite3.connect(store) as db: db.execute("PRAGMA user_version=0")
    elif state == "legacy-v4": shutil.copyfile(mcp.LEGACY_V4, store)
    elif state == "ready": mcp.memory(store).bootstrap()
    elif state == "v2":
        # Use the immutable write corpus's pre-migration v2 fixture builder.
        from tools.golden.generate import legacy_v2
        legacy_v2(store)
    elif state == "v3":
        from tools.golden.generate import legacy_v3
        legacy_v3(store)
    elif state in {"foreign", "future"}: mcp.setup_incompatible(store, future=state == "future")
    elif state == "wal": mcp.setup_wal_sidecar(store)
    elif state == "failed-check":
        mcp.memory(store).bootstrap()
        with sqlite3.connect(store) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO memory_revision_evidence_v3(revision_id,evidence_id,stance,weight,reason) VALUES('orphan','orphan','supports',1.0,'forced doctor failure')")


COMMANDS = {"init": [], "setup": [], "profiles": [], "work": [], "status": [],
    "retrieve": ["who are you"], "log-phase": ["cli-phase", "--title", "Phase", "--summary", "CLI phase", "--open-loop"],
    "log-fact": ["cli-fact", "--title", "Fact", "--summary", "CLI fact"], "timeline": [], "decay": [],
    "core-proposals": [], "approve-core": ["core-proposal:missing", "--apply"],
    "approve-retract": ["phase:missing", "--reason", "owner"], "close-legacy-discussion": ["--note", "owner"],
    "apply-receipt": ["receipt:missing"], "close-loop": ["phase:missing", "--note", "owner"],
    "migrate-to": ["target.sqlite3"], "doctor": []}


def scenario_matrix():
    scenarios = [{"name": "p16-store-busy", "state": "ready", "argv": ["--db", "store.sqlite3", "log-phase", "blocked", "--title", "Blocked", "--summary", "Blocked"]}]
    for command, args in COMMANDS.items():
        for state in ("missing", "uninitialized", "legacy-v4", "ready"):
            scenarios.append({"name": f"{command}-{state}", "state": state, "argv": ["--db", "store.sqlite3", command, *args]})
    for state in ("v2", "v3", "foreign", "future", "wal", "failed-check"):
        scenarios.append({"name": f"doctor-{state}", "state": state, "argv": ["--db", "store.sqlite3", "doctor"]})
    for flag in ("--packet", "--readonly", "--history"):
        scenarios.append({"name": "retrieve-"+flag[2:], "state": "ready", "argv": ["--db", "store.sqlite3", "retrieve", "who are you", flag]})
    for index, token in enumerate(["١٢", "１２", "१२", "1_2", "_1", "1_", "1__2", "\u20031\u3000", "+1", "-1", "²", "\x1c1\x1f", "9007199254740993", "9"*400]):
        scenarios.append({"name": f"integer-{index:02}", "argv": ["--db", "store.sqlite3", "timeline", "--limit", token]})
    for index, argv in enumerate([["--help"], ["retrieve", "--help"], [], ["bad-command"], ["retrieve"], ["approve-core", "x", "--apply", "--reject"]]):
        scenarios.append({"name": f"usage-{index:02}", "argv": argv})
    for mode in ("name", "folder", "json", "url", "env", "last", "default", "failure", "profiles", "overwrite-json", "overwrite-url", "safe-name", "surrogate", "invalid-json", "invalid-utf8", "url-large", "url-invalid-core", "url-invalid-json", "url-invalid-utf8"):
        scenarios.append({"name": f"resolve-{mode}", "resolve": mode, "argv": ["--db", "store.sqlite3", "status"]})
    for kind in ("nan", "infinity", "negative-infinity", "high-surrogate", "low-surrogate", "paired-surrogate"):
        for command in ("status", "profiles"):
            scenarios.append({"name": f"decode-profile-{kind}-{command}", "decode_profile": kind,
                              "argv": ["--db", "store.sqlite3", command]})
        scenarios.append({"name": f"decode-install-{kind}", "decode_profile": kind, "install": True,
                          "argv": ["--db", "store.sqlite3", "status"]})
    for mode in ("missing-dry", "missing-backup-equal", "v2", "v3", "v4", "existing-target", "existing-backup", "equal-backup", "dry-parent"):
        state = "missing" if mode.startswith("missing") else mode if mode in {"v2", "v3"} else "legacy-v4"
        argv = ["--db", "store.sqlite3", "migrate-to", "target.sqlite3"]
        if mode == "missing-dry": argv += ["--dry-run"]
        if mode.endswith("equal") or mode == "equal-backup": argv += ["--backup", "target.sqlite3"]
        if mode == "existing-backup": argv += ["--backup", "backup.sqlite3"]
        if mode == "dry-parent": argv = ["--db", "store.sqlite3", "migrate-to", "nested/parent/target.sqlite3", "--dry-run"]
        scenarios.append({"name": "migration-"+mode, "state": state, "migration": mode, "argv": argv})
    for mode in ("core-apply", "core-reject", "retract", "legacy-close"):
        for outcome in ("inline", "issue-only", "inline-failure", "no-tty", "mismatch", "invalid-utf8"):
            scenarios.append({"name": f"authority-{mode}-{outcome}", "authority": mode, "outcome": outcome})
    for mode in ("receipt-integrity", "proposal-integrity", "proposal-decided", "stale-authority"):
        scenarios.append({"name": f"error-{mode}", "error": mode})
    scenarios.append({"name": "error-owner-pinned-target", "state": "ready", "argv": ["--db", "store.sqlite3", "approve-retract", "core", "--reason", "owner"]})
    scenarios.append({"name": "error-untyped-crash", "state": "ready", "hook": "untyped-crash", "argv": ["--db", "store.sqlite3", "status"]})
    for mode in ("present", "precedence", "invalid"):
        scenarios.append({"name": "work-"+mode, "work": mode, "argv": ["--db", "store.sqlite3", "work"]})
    return sorted(scenarios, key=lambda x: x["name"])


def prepare(work, scenario):
    shutil.copytree(BUNDLED, work / "profiles")
    store_state(work, scenario.get("state", "missing"))
    argv = scenario.get("argv", [])
    env = {}
    stdin = b""
    tty = False
    hook = scenario.get("hook", "")
    mode = scenario.get("resolve")
    if mode:
        data = json.loads((BUNDLED / "example/profile.json").read_text())
        data["name"] = "remote"
        data["core"]["summary"] = "installed from CLI"
        (work / "recipe.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        if mode in {"json", "overwrite-json"}: argv = ["-p", "recipe.json", *argv]
        elif mode in {"url", "overwrite-url", "url-large", "url-invalid-core", "url-invalid-json", "url-invalid-utf8"}: argv = ["-p", "{{ORIGIN}}/profile.json", *argv]
        elif mode == "folder": argv = ["-p", "profiles/example", *argv]
        elif mode == "name": argv = ["-p", "example", *argv]
        elif mode == "env": env["TRAJECTA_IDENTITY_PROFILE"] = "example"
        elif mode == "last":
            (work / "data").mkdir(); (work / "data/.last-profile").write_text("example\n")
        elif mode == "failure":
            del data["core"]["falsifier"]
            (work / "recipe.json").write_text(json.dumps(data), encoding="utf8")
            (work / "data").mkdir(); (work / "data/.last-profile").write_text("example\n")
            argv = ["-p", "recipe.json", *argv]
        elif mode == "profiles": argv = ["profiles"]
        elif mode in {"safe-name", "surrogate"}:
            data["name"] = "AUX.é１２😀" if mode == "safe-name" else "surrogate"
            if mode == "surrogate":
                data["core"]["summary"] = "display \ud800 only"
                (work / "profiles/surrogate").mkdir()
                (work / "profiles/surrogate/profile.json").write_text(json.dumps(data), encoding="utf8")
                argv = ["profiles"]
            else:
                (work / "recipe.json").write_text(json.dumps(data), encoding="utf8")
                argv = ["-p", "recipe.json", "doctor"]
        if mode in {"invalid-json", "invalid-utf8"}:
            (work / "recipe.json").write_bytes(b"{broken" if mode == "invalid-json" else b"\xff")
            argv = ["-p", "recipe.json", *argv]
        if mode == "url-large": (work / "recipe.json").write_bytes(b" " * (256*1024+1))
        if mode == "url-invalid-json": (work / "recipe.json").write_bytes(b"{broken")
        if mode == "url-invalid-utf8": (work / "recipe.json").write_bytes(b"\xff")
        if mode == "url-invalid-core":
            del data["core"]["falsifier"]
            (work / "recipe.json").write_text(json.dumps(data), encoding="utf8")
        if mode.startswith("overwrite"):
            dest = work / "data/profiles/remote"; dest.mkdir(parents=True)
            old = dict(data); old["core"] = dict(data["core"], summary="old installed contents")
            (dest / "profile.json").write_text(json.dumps(old), encoding="utf8")
    kind = scenario.get("decode_profile")
    if kind:
        data = json.loads((BUNDLED / "example/profile.json").read_text())
        data["name"] = "extended"
        if kind in {"nan", "infinity", "negative-infinity"}:
            data["v"] = {"nan": float("nan"), "infinity": float("inf"), "negative-infinity": float("-inf")}[kind]
        else:
            data["name"] = {"high-surrogate": "\ud800", "low-surrogate": "\udfff", "paired-surrogate": "😀"}[kind]
            data["core"]["summary"] = "display " + data["name"]
        if scenario.get("install"):
            (work / "recipe.json").write_text(json.dumps(data, ensure_ascii=True), encoding="utf8")
            argv = ["-p", "recipe.json", *argv]
        else:
            folder = work / "profiles/extended"; folder.mkdir()
            (folder / "profile.json").write_text(json.dumps(data, ensure_ascii=True), encoding="utf8")
            if "status" in argv: argv = ["-p", "extended", *argv]
    migration = scenario.get("migration")
    if migration == "existing-target": (work / "target.sqlite3").write_bytes(b"target")
    if migration == "existing-backup": (work / "backup.sqlite3").write_bytes(b"backup")
    error = scenario.get("error")
    if error:
        state = getattr(mcp, "setup_" + {"receipt-integrity": "receipt_integrity", "proposal-integrity": "proposal_integrity", "proposal-decided": "proposal_decided", "stale-authority": "stale"}[error])(work / "store.sqlite3")
        argv = ["--db", "store.sqlite3", "apply-receipt", state["receipt_id"]]
    authority = scenario.get("authority")
    if authority:
        mem = mcp.memory(work / "store.sqlite3"); mem.bootstrap()
        if authority.startswith("core"):
            proposal = mem.identity_core_propose(reason="CLI proposal", phase_context={"model": "oracle", "harness": "cli"})
            choice = "--apply" if authority == "core-apply" else "--reject"
            argv = ["--db", "store.sqlite3", "approve-core", proposal["proposal_id"], choice]
            expected = ("APPLY" if choice == "--apply" else "REJECT") + " " + mcp.short(proposal["proposal_id"])
        elif authority == "retract":
            mem.log_phase("retract-me", title="Retract", summary="CLI fixture")
            argv = ["--db", "store.sqlite3", "approve-retract", "phase:retract-me", "--reason", "owner"]
            expected = "RETRACT phase:retract-me"
        else:
            mem.close if hasattr(mem, "close") else None
            mcp.setup_legacy_close(work / "store.sqlite3")
            mem = mcp.memory(work / "store.sqlite3")
            with mem.store.connect(readonly=True) as db:
                event = db.execute("SELECT relation_event_id FROM memory_relation_events_v4 WHERE relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1").fetchone()[0]
            expected = "CLOSE " + mcp.short(event)
            argv = ["--db", "store.sqlite3", "close-legacy-discussion", "--note", "CLI close"]
        outcome = scenario["outcome"]
        tty = outcome != "no-tty"
        stdin = (("WRONG" if outcome == "mismatch" else expected) + "\n").encode()
        if outcome == "invalid-utf8": stdin = b"\xff\n"
        if outcome == "issue-only": argv += ["--issue-only"]
        if outcome == "inline-failure": hook = "inline-failure"
    if scenario.get("work"):
        mode = scenario["work"]
        if mode == "present": mcp.setup_work_env(work / "store.sqlite3"); env["TRAJECTA_WORK_ROOT"] = "{{ROOT}}/work"
        elif mode == "precedence":
            mcp._work_dir(work / "env-work", "work:aaaaaaaa-0001", "env")
            mcp._work_dir(work / "profile-work", "work:bbbbbbbb-0002", "profile")
            data = json.loads((work / "profiles/example/profile.json").read_text()); data["work_root"] = "profile-work"
            (work / "profiles/example/profile.json").write_text(json.dumps(data), encoding="utf8")
            env["TRAJECTA_WORK_ROOT"] = "{{ROOT}}/env-work"
        else:
            (work / "work").mkdir(); (work / "work/state.json").write_text('{"schema":"wrong","work":[]}')
            env["TRAJECTA_WORK_ROOT"] = "{{ROOT}}/work"
    return argv, env, stdin, tty, hook


def validate_paths(argv, env, work):
    for index, value in enumerate(argv):
        if any(value.startswith(option + "=") for option in ("--db", "--backup", "--out")):
            assert Path(value.split("=", 1)[1]).expanduser().resolve().is_relative_to(work)
        if value in {"--db", "--backup", "--out"}:
            assert Path(argv[index+1]).expanduser().resolve().is_relative_to(work)
        if value == "migrate-to": assert Path(argv[index+1]).resolve().is_relative_to(work)
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA", "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES"):
        assert Path(env[key]).resolve().is_relative_to(work)


def generate(output):
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("CLI corpus generation requires Python 3.11 / Unicode 14.0.0")
    if output.exists() and any(output.iterdir()): raise SystemExit("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    hashes = write_tables(output)
    write_json(output / "decode-errors.json", differential_table(ORACLE_COMMIT))
    isolation = ROOT / ".isolation"; isolation.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cli-golden-", dir=isolation) as temporary:
        workspace = Path(temporary).resolve()
        for config in scenario_matrix():
            name = config["name"]; scenario = output / name; scenario.mkdir()
            work = workspace / name; work.mkdir(); work = work.resolve()
            clock = workspace / (name + "-clock"); clock.mkdir()
            mcp.write_sitecustomize(clock / "sitecustomize.py")
            with clock_context(): argv, extra, stdin, tty, hook = prepare(work, config)
            file_times = {}
            if "migrate-to" in argv and "--db" in argv:
                source = argv[argv.index("--db") + 1]
                if (work / source).exists():
                    # A declared exact-second fixture mtime is representable on the pinned
                    # Docker bind mount and all three replay filesystems. No output tolerance.
                    file_times[source] = 1700000000
                    os.utime(work / source, (1700000000, 1700000000))
            if tty or hook:
                with (clock / "sitecustomize.py").open("a") as out:
                    if tty: out.write("\nimport sys\nsys.stdin.isatty = lambda: True\nsys.stdout.isatty = lambda: True\n")
                    if hook == "inline-failure": out.write("\nfrom trajecta_identity.identity import IdentityMemory\nfrom trajecta_identity.authority import StaleAuthority\ndef fail(self, receipt): raise StaleAuthority('target moved after issuance')\nIdentityMemory.identity_core_apply = fail\nIdentityMemory.identity_retract = fail\nIdentityMemory.identity_close_legacy_discussion = fail\n")
                    if hook == "untyped-crash": out.write("\nfrom trajecta_identity.identity import IdentityMemory\ndef fail(self): raise RuntimeError('test internal crash')\nIdentityMemory.status = fail\n")
            env = isolated_env(work, clock); env.update(extra)
            # Stable input templates contain only fixture-relative paths/tokens.
            input_env = {key: value.replace(str(work), "{{ROOT}}") for key, value in env.items() if key not in {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "SYSTEMROOT", "COMSPEC", "PYTHONHASHSEED"}}
            write_json(scenario / "argv.json", argv); write_json(scenario / "env.json", input_env)
            (scenario / "stdin").write_bytes(stdin)
            write_json(scenario / "invocation.json", {"tty": tty, "hook": hook, "file_times": file_times})
            initial = snapshot(work)
            shutil.copytree(work, scenario / "fixture")
            body = (work / "recipe.json").read_bytes() if (work / "recipe.json").exists() else b"{}"
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(body)
                def log_message(self, *args): pass
            server = None; thread = None; origin = ""
            if any("{{ORIGIN}}" in value for value in [*argv, *input_env.values()]):
                server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                origin = f"http://127.0.0.1:{server.server_port}"
            records = []
            for file in ("argv.json", "env.json"):
                raw = (scenario / file).read_bytes()
                records.extend({"file": file, "byte_offset": m.start(), "token": m[0].decode(), "context": "json-string"}
                    for m in __import__('re').finditer(rb"\{\{(?:ROOT|ORIGIN)\}\}", raw))
            live_argv = json.loads(render((scenario / "argv.json").read_bytes(), records, file="argv.json", root=str(work), origin=origin))
            live_env = json.loads(render((scenario / "env.json").read_bytes(), records, file="env.json", root=str(work), origin=origin))
            env.update(live_env)
            old_cwd = Path.cwd()
            try:
                os.chdir(work); validate_paths(live_argv, env, work)
                from contextlib import nullcontext
                from tools.r3.busy import held_writer
                busy = name == "p16-store-busy"
                with held_writer("py", work / "store.sqlite3", env) if busy else nullcontext():
                    before_busy = (work / "store.sqlite3").read_bytes() if busy else None
                    import time
                    start = time.monotonic()
                    process = subprocess.run([sys.executable, "-m", "trajecta_identity.cli", *live_argv], cwd=work, env=env, input=stdin, capture_output=True)
                    if busy:
                        assert time.monotonic() - start >= 4.5
                        assert process.returncode == 1 and process.stderr == b"StoreBusy: store is busy; retry later\n"
                        assert (work / "store.sqlite3").read_bytes() == before_busy
            finally:
                os.chdir(old_cwd)
                if server:
                    server.shutdown(); server.server_close(); thread.join()
            mode = "usage" if process.returncode == 2 else "help" if "--help" in argv or "-h" in argv else "exact"
            if process.returncode == 1 and b"Traceback (most recent call last)" in process.stderr: mode = "crash"
            for file, raw in (("stdout", process.stdout), ("stderr", process.stderr)):
                if mode in {"help", "usage", "crash"} and (file == "stderr" or mode == "help"):
                    # Prose/tracebacks are explicitly not frozen (§3.1/A13).
                    raw = usage_prefix(process.stderr) if mode == "usage" else b"nonempty" if mode == "crash" else b""
                if config.get("argv", [None])[-1:] == ["setup"] or "setup" in argv:
                    if file == "stdout" and process.returncode == 0:
                        data = json.loads(raw)
                        for launcher in data["mcpServers"].values():
                            assert launcher["args"][:2] == ["-m", "trajecta_identity.mcp_server"]
                            assert launcher["command"] == sys.executable  # launcher validated separately (§3.3)
                        raw = (json.dumps(data, ensure_ascii=False, indent=2)+"\n").encode()
                context = "json-string" if file == "stdout" and not tty and "--packet" not in argv else "raw-text"
                encoded, listing = template(raw, file=file, root=str(work), origin=origin, context=context)
                (scenario / file).write_bytes(encoded); records.extend(listing)
            post = snapshot(work); dumps = {}
            for relative, info in list(post.items()):
                path = work / relative
                if not path.is_file(): continue
                if relative == "data/.last-profile":
                    raw, listing = template(path.read_bytes(), file=relative, root=str(work), origin=origin, context="raw-text")
                    (scenario / "last-profile").write_bytes(raw); records.extend(listing)
                    info.clear(); info.update(kind="file", template="last-profile")
                elif relative.endswith(".sqlite3") and path.stat().st_size > 100 and path.read_bytes().startswith(b"SQLite format 3"):
                    dump_path = path
                    if path.with_name(path.name + "-wal").exists():
                        dump_path = clock / "dump.sqlite3"
                        shutil.copyfile(path, dump_path)
                    dumps[relative] = dump_database(dump_path); info.clear(); info.update(kind="store", dump=relative)
                    if initial.get(relative, {}).get("sha256") == sha(path.read_bytes()):
                        info["unchanged_sha256"] = sha(path.read_bytes())
                else:
                    template(path.read_bytes(), file=relative, root=str(work), origin=origin, context="raw-text")
            write_json(scenario / "tokens.json", records)
            write_json(scenario / "expected.json", {"exit": process.returncode, "comparison": mode, "tree": post, "dumps": dumps, "databases": database_classes(work, initial, live_argv)})
    sources = {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for pattern in (*mcp.SOURCES[:-1], "tools/golden_cli/*.py") for p in sorted(ROOT.glob(pattern))}
    files = {p.relative_to(output).as_posix(): sha(p.read_bytes()) for p in sorted(output.rglob("*")) if p.is_file()}
    write_json(output / "MANIFEST.json", {"schema": "trajecta.golden-cli-manifest/v1", "oracle_commit": ORACLE_COMMIT,
        "oracle_semantics": ORACLE_SEMANTICS, "oracle_sources": sources, "python": sys.version.split()[0],
        "unicode": unicodedata.unidata_version, "sqlite": sqlite3.sqlite_version, "files": files, "table_hashes": hashes})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, default=ROOT / "spec/golden-cli-v1")
    generate(parser.parse_args().output)
