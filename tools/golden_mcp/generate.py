"""Generate the deterministic R2c stdio MCP oracle corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memory_core import MemoryStore  # noqa: E402
from tools.golden.generate import clock_context, dump_database  # noqa: E402
from trajecta_identity import IdentityMemory, load_profile  # noqa: E402
from trajecta_identity.paths import profile_db, safe_fs_name  # noqa: E402

ORACLE_COMMIT = "2916d15ba11508f7e5155568ff281cfdb30cb3fd"
ORACLE_SEMANTICS = [
    "R0-frozen",
    "R2a-authority-v2",
    "R2b-writers",
    "P6-raw-byte-boundary",
    "P7-recursive-input-validation",
    "P8-retrieve-tracking-contract",
    "P9-explicit-bootstrap-policy",
    "P16-store-busy",
    "P15-evidence-metadata",
    "P17-sequence-allocation",
    "R3-backup-microsecond-domain",
]
TABLES = ROOT / "memory_core" / "tables"
LEGACY_V4 = ROOT / "spec" / "golden" / "identity-open" / "store.sqlite3"
BUNDLED_PROFILES = ROOT / "trajecta_identity" / "profiles"
SOURCES = (
    "memory_core/*.py",
    "memory_core/schema.sql",
    "trajecta_identity/*.py",
    "trajecta_identity/profiles/example/profile.json",
    "tools/golden_mcp/generate.py",
    "tools/r3/busy.py",
    "tools/r3/holder.py",
    "tools/golden/generate.py",
)


class TTY:
    def __init__(self, value: str):
        self.value = value

    def isatty(self):
        return True

    def readline(self):
        return self.value + "\n"

    def write(self, value):
        return len(value)

    def flush(self):
        pass


def request(request_id: str, method: str, params: str | None = None) -> bytes:
    suffix = "" if params is None else f',"params":{params}'
    return f'{{"jsonrpc":"2.0","id":{request_id},"method":"{method}"{suffix}}}'.encode()


def tool(request_id: int, name: str, arguments: str | None = None) -> bytes:
    suffix = "" if arguments is None else f',"arguments":{arguments}'
    return request(str(request_id), "tools/call", f'{{"name":"{name}"{suffix}}}')


def lines(*frames: bytes, final_newline: bool = True) -> bytes:
    result = b"\n".join(frames)
    return result + (b"\n" if final_newline else b"")


def short(value: str) -> str:
    return value.split(":", 1)[-1][:12]


def memory(path: Path, profile=None) -> IdentityMemory:
    return IdentityMemory(profile or load_profile(BUNDLED_PROFILES / "example"), path, surface="mcp")


def issue_core(mem: IdentityMemory, proposal: dict[str, Any], *, outcome="apply", note="") -> dict[str, Any]:
    terminal = TTY(f"{outcome.upper()} {short(proposal['proposal_id'])}")
    return mem.issue_core_receipt(
        proposal["proposal_id"],
        outcome=outcome,
        decision_note=note,
        stdin=terminal,
        stdout=terminal,
    )


def setup_apply(path: Path) -> dict[str, str]:
    mem = memory(path)
    mem.bootstrap()
    proposal = mem.identity_core_propose(reason="MCP apply", phase_context={"model": "oracle", "harness": "mcp"})
    return {"receipt_id": issue_core(mem, proposal)["receipt_id"]}


def setup_retract(path: Path) -> dict[str, str]:
    mem = memory(path)
    mem.bootstrap()
    mem.log_phase("retract-me", title="Retract me", summary="MCP fixture")
    terminal = TTY("RETRACT phase:retract-me")
    receipt = mem.issue_retract_receipt(
        "phase:retract-me",
        reason="owner correction",
        stdin=terminal,
        stdout=terminal,
    )
    return {"receipt_id": receipt["receipt_id"]}


def setup_legacy_close(path: Path) -> dict[str, str]:
    shutil.copyfile(LEGACY_V4, path)
    target = path.with_name("migrated.sqlite3")
    backup = path.with_name("backup.sqlite3")
    MemoryStore(path).migrate_to(target, backup_path=backup)
    path.unlink()
    target.rename(path)
    backup.unlink()
    mem = memory(path)
    with mem.store.connect(readonly=True) as connection:
        relation = connection.execute(
            "SELECT relation_event_id FROM memory_relation_events_v4 "
            "WHERE relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
    terminal = TTY(f"CLOSE {short(relation['relation_event_id'])}")
    receipt = mem.issue_legacy_close_receipt(note="settled", stdin=terminal, stdout=terminal)
    return {"receipt_id": receipt["receipt_id"]}


def setup_receipt_integrity(path: Path) -> dict[str, str]:
    result = setup_apply(path)
    mem = memory(path)
    with mem.store._raw_connect() as connection:
        connection.execute("DROP TRIGGER memory_owner_receipts_v5_no_update")
        connection.execute(
            "UPDATE memory_owner_receipts_v5 SET binding_sha256=? WHERE receipt_id=?",
            ("0" * 64, result["receipt_id"]),
        )
    return result


def setup_proposal_integrity(path: Path) -> dict[str, str]:
    result = setup_apply(path)
    mem = memory(path)
    with mem.store._raw_connect() as connection:
        connection.execute("DROP TRIGGER memory_core_proposals_v5_no_update")
        connection.execute("UPDATE memory_core_proposals_v5 SET content=content||' '")
    return result


def setup_proposal_decided(path: Path) -> dict[str, str]:
    mem = memory(path)
    mem.bootstrap()
    proposal = mem.identity_core_propose(reason="MCP decided", phase_context={"model": "oracle"})
    first = issue_core(mem, proposal, outcome="reject", note="one")
    second = issue_core(mem, proposal, outcome="reject", note="two")
    mem.identity_core_apply(first["receipt_id"])
    return {"receipt_id": second["receipt_id"]}


def setup_stale(path: Path) -> dict[str, str]:
    mem = memory(path)
    mem.bootstrap()
    first = mem.identity_core_propose(reason="MCP first", phase_context={"model": "one"})
    second = mem.identity_core_propose(reason="MCP second", phase_context={"model": "two"})
    stale = issue_core(mem, second, outcome="reject")
    winner = issue_core(mem, first)
    mem.identity_core_apply(winner["receipt_id"])
    return {"receipt_id": stale["receipt_id"]}


def setup_v4(path: Path) -> dict[str, str]:
    shutil.copyfile(LEGACY_V4, path)
    return {}


def setup_incompatible(path: Path, *, future: bool) -> dict[str, str]:
    mem = memory(path)
    mem.bootstrap()
    with sqlite3.connect(path) as connection:
        if future:
            connection.execute("PRAGMA user_version=99")
        else:
            connection.execute("PRAGMA application_id=12345")
    return {}


def setup_wal_sidecar(path: Path) -> dict[str, str]:
    memory(path).bootstrap()
    path.with_name(path.name + "-wal").write_bytes(b"WAL fixture")
    return {}


def setup_work_error(path: Path) -> dict[str, str]:
    profile = json.loads((ROOT / "trajecta_identity" / "profiles" / "example" / "profile.json").read_text())
    profile["name"] = "mcp-work-error"
    profile["work_root"] = "work"
    path.parent.joinpath("profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    work = path.parent / "work"
    work.mkdir()
    work.joinpath("state.json").write_text('{"schema":"wrong","work":[]}\n', encoding="utf-8")
    return {"profile": "profile.json"}


def initialize_transcript(_: dict[str, str]) -> bytes:
    frames = [
        request(index, "initialize", f'{{"protocolVersion":"{version}"}}')
        for index, version in enumerate(("2025-06-18", "2025-03-26", "2024-11-05"), 1)
    ]
    frames.extend(
        [
            request("4", "initialize", '{"protocolVersion":"2099-01-01"}'),
            request("5", "initialize", "{}"),
            request("6", "initialize", '{"protocolVersion":1}'),
            request(
                "7",
                "initialize",
                '{"protocolVersion":"2025-06-18","clientInfo":{"name":"client"},"capabilities":{"x":true}}',
            ),
        ]
    )
    return lines(*frames)


def basic_transcript(_: dict[str, str]) -> bytes:
    return lines(
        request("1", "ping"),
        request("2", "tools/list"),
        request("3", "unknown"),
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"identity_log_phase",'
        b'"arguments":{"event_id":"notification","title":"No","summary":"No"}}}',
        request("4", "notifications/foo"),
    )


def ids_transcript(_: dict[str, str]) -> bytes:
    return lines(
        request("0", "ping"),
        request("9007199254740993", "ping"),
        request('"x"', "ping"),
        request("null", "ping"),
        request("1.0", "ping"),
        request("true", "ping"),
        request("{}", "ping"),
        request("[]", "ping"),
        request(r'"\ud800"', "ping"),
        br'{"jsonrpc":"2.0","id":"safe","method":"ping","extra":"\ud800"}',
    )


def framing_transcript(_: dict[str, str]) -> bytes:
    return (
        request("1", "ping")
        + b"\n\xff\n"
        + request("2", "ping")
        + b"\r\n \t\r\n{\n[]\n"
        + b'{"id":3,"method":"ping"}\n'
        + b'{"jsonrpc":"1.0","id":4,"method":"ping"}\n'
        + b'{"jsonrpc":"2.0","id":5,"method":"ping","params":[]}\n'
        + request("6", "ping")
    )


def float_bounds_transcript(_: dict[str, str]) -> bytes:
    # Overflowing float tokens are a parse error (-32700, id null) before any
    # request/id validation; the largest finite binary64 value still parses.
    phase = '{"event_id":"float-max","title":"Float","summary":"bounds","phase_context":{"x":%s}}'
    return lines(
        tool(1, "identity_log_phase", phase % "1e400"),
        request("1e400", "ping"),
        tool(2, "identity_log_phase", phase % "-1e400"),
        tool(3, "identity_log_phase", phase % "1.7976931348623157e308"),
    )


def structure_transcript(_: dict[str, str]) -> bytes:
    return lines(
        request("1", "tools/call", "{}"),
        request("2", "tools/call", '{"name":1}'),
        request("3", "tools/call", '{"name":"identity_status","arguments":[]}'),
        request("4", "tools/call", '{"name":"unknown","arguments":{}}'),
    )


def validation_transcript(_: dict[str, str]) -> bytes:
    too_long = "x" * 2001
    too_many = json.dumps(["x"] * 21, separators=(",", ":"))
    cases = [
        '{"cue":"x","extra":1}',
        "{}",
        '{"cue":1}',
        '{"cue":""}',
        json.dumps({"cue": too_long}, separators=(",", ":")),
        '{"cue":"x","limit":0}',
        '{"cue":"x","limit":25}',
        '{"cue":"x","limit":10.0}',
        '{"cue":"x","limit":"10"}',
        '{"cue":"x","limit":true}',
        '{"cue":"x","track":1}',
    ]
    frames = [tool(index, "identity_retrieve", value) for index, value in enumerate(cases, 1)]
    frames.extend(
        [
            tool(20, "identity_log_phase", '{"event_id":"e","title":"t","summary":"s","phase_context":[]}'),
            tool(21, "identity_log_phase", '{"event_id":"e","title":"t","summary":"s","follows":{}}'),
            tool(22, "identity_log_phase", f'{{"event_id":"e","title":"t","summary":"s","follows":{too_many}}}'),
            tool(23, "identity_log_phase", '{"event_id":"e","title":"t","summary":"s","follows":["ok",1]}'),
            tool(
                24,
                "identity_core_propose",
                '{"reason":"r","phase_context":{"free":[1,1.0]},'
                '"vho_stack":{"runtime_architecture":1}}',
            ),
            tool(
                25,
                "identity_core_propose",
                '{"reason":"r","phase_context":{},"vho_stack":{"unknown":"x"}}',
            ),
        ]
    )
    return lines(*frames)


def missing_reads_transcript(_: dict[str, str]) -> bytes:
    return lines(
        tool(1, "identity_status", "{}"),
        tool(2, "identity_retrieve", '{"cue":"empty","track":true}'),
        tool(3, "identity_retrieve", '{"cue":"empty","track":false}'),
        tool(4, "identity_timeline", "{}"),
        tool(5, "identity_core_proposals", "{}"),
    )


def invalid_mutation_transcript(_: dict[str, str]) -> bytes:
    return lines(tool(1, "identity_log_phase", '{"event_id":"missing"}'))


def positive_tools_transcript(_: dict[str, str]) -> bytes:
    return lines(
        tool(
            1,
            "identity_log_phase",
            '{"event_id":"phase-one","title":"Phase","summary":"Open loop","open_loop":true,'
            '"phase_context":{"2":2,"1":1.0}}',
        ),
        tool(2, "identity_log_fact", '{"fact_id":"int-confidence","title":"Integer","summary":"typed","confidence":1}'),
        tool(
            3,
            "identity_log_fact",
            '{"fact_id":"float-confidence","title":"Float","summary":"typed","confidence":1.0}',
        ),
        tool(
            4,
            "identity_core_propose",
            '{"reason":"new reading","phase_context":{"model":"oracle","harness":"mcp"}}',
        ),
        tool(5, "identity_core_proposals", "{}"),
        tool(6, "identity_retrieve", '{"cue":"phase","track":false}'),
        tool(7, "identity_retrieve", '{"cue":"phase"}'),
        tool(8, "identity_timeline", "{}"),
        tool(9, "identity_status", "{}"),
        tool(10, "identity_close_loop", '{"record_id":"phase:phase-one","note":"settled"}'),
        tool(11, "identity_core_apply", '{"receipt_id":"receipt:00000000000000000000000000000000"}'),
        tool(12, "identity_core_propose", '{"reason":"domain validation","phase_context":{}}'),
    )


def receipt_transcript(name: str) -> Callable[[dict[str, str]], bytes]:
    def build(state: dict[str, str]) -> bytes:
        return lines(tool(1, name, json.dumps({"receipt_id": state["receipt_id"]}, separators=(",", ":"))))

    return build


def v4_reads_transcript(_: dict[str, str]) -> bytes:
    return lines(
        tool(1, "identity_status", "{}"),
        tool(2, "identity_retrieve", '{"cue":"who are you"}'),
        tool(3, "identity_timeline", "{}"),
        tool(4, "identity_core_proposals", "{}"),
        tool(5, "identity_log_phase", '{"event_id":"blocked","title":"Blocked","summary":"Blocked"}'),
    )


WORK_ITEM = (
    '{"id":"%s","topic":"%s","goal":"parity","status":"active","revision":3,'
    '"nextAction":"replay","openLoops":["crash"],"activeBranchId":"branch:one",'
    '"branches":[{"id":"branch:one","label":"writers"}],"updatedAt":"2026-09-30T00:00:00Z"}'
)


def _work_dir(root: Path, item_id: str, topic: str) -> None:
    root.mkdir()
    root.joinpath("state.json").write_text(
        '{"schema":"trajecta.state/v1","work":[' + WORK_ITEM % (item_id, topic) + "]}\n", encoding="utf-8"
    )


def _profile_with_work_root(path: Path, name: str, work_root: str | None) -> str:
    profile = json.loads((ROOT / "trajecta_identity" / "profiles" / "example" / "profile.json").read_text())
    profile["name"] = name
    profile.pop("work_root", None)
    if work_root is not None:
        profile["work_root"] = work_root
    path.parent.joinpath("profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return "profile.json"


def setup_work_env(path: Path) -> dict[str, Any]:
    _work_dir(path.parent / "work", "work:12345678-abcd", "env-only")
    return {
        "profile": _profile_with_work_root(path, "mcp-work-env", None),
        "env": {"TRAJECTA_WORK_ROOT": "work"},
        "fixture": True,
    }


def setup_work_precedence(path: Path) -> dict[str, Any]:
    _work_dir(path.parent / "env-work", "work:aaaaaaaa-0001", "from-env")
    _work_dir(path.parent / "profile-work", "work:bbbbbbbb-0002", "from-profile")
    return {
        "profile": _profile_with_work_root(path, "mcp-work-precedence", "profile-work"),
        "env": {"TRAJECTA_WORK_ROOT": "env-work"},
        "fixture": True,
    }


def setup_work_blank_env(path: Path) -> dict[str, Any]:
    state = setup_work_precedence(path)
    state["env"] = {"TRAJECTA_WORK_ROOT": " \t "}
    return state


def work_env_transcript(_: dict[str, Any]) -> bytes:
    phase = '{"event_id":"%s","title":"Work","summary":"linked work","work_refs":["%s"]}'
    return lines(
        tool(1, "identity_status", "{}"),
        tool(2, "identity_log_phase", phase % ("with-work", "work:12345678-abcd")),
        tool(3, "identity_log_phase", phase % ("missing-work", "work:deadbeef-0000")),
        tool(4, "identity_retrieve", '{"cue":"linked work","track":false}'),
    )


def work_precedence_transcript(_: dict[str, Any]) -> bytes:
    phase = '{"event_id":"%s","title":"Work","summary":"linked work","work_refs":["%s"]}'
    return lines(
        tool(1, "identity_status", "{}"),
        tool(2, "identity_log_phase", phase % ("env-ref", "work:aaaaaaaa-0001")),
        tool(3, "identity_log_phase", phase % ("profile-ref", "work:bbbbbbbb-0002")),
        tool(4, "identity_retrieve", '{"cue":"linked work","track":false}'),
    )


def mutation_error_transcript(_: dict[str, str]) -> bytes:
    return lines(tool(1, "identity_log_phase", '{"event_id":"blocked","title":"Blocked","summary":"Blocked"}'))


def work_error_transcript(_: dict[str, str]) -> bytes:
    return lines(
        tool(
            1,
            "identity_log_phase",
            '{"event_id":"work-error","title":"Work","summary":"Bad work","work_refs":["work:12345678-abcd"]}',
        )
    )


Setup = Callable[[Path], dict[str, str]]
Transcript = Callable[[dict[str, str]], bytes]


SCENARIOS: dict[str, tuple[Setup | None, Transcript]] = {
    "p16-store-busy": (lambda path: (memory(path).bootstrap() and {}) or {}, mutation_error_transcript),
    "initialize": (None, initialize_transcript),
    "basic-methods": (None, basic_transcript),
    "ids": (None, ids_transcript),
    "framing": (None, framing_transcript),
    "call-structure": (None, structure_transcript),
    "float-bounds": (None, float_bounds_transcript),
    "validation": (None, validation_transcript),
    "missing-reads": (None, missing_reads_transcript),
    "invalid-mutation": (None, invalid_mutation_transcript),
    "positive-tools": (None, positive_tools_transcript),
    "authority-apply": (setup_apply, receipt_transcript("identity_core_apply")),
    "authority-retract": (setup_retract, receipt_transcript("identity_retract")),
    "authority-legacy-close": (setup_legacy_close, receipt_transcript("identity_close_legacy_discussion")),
    "error-receipt-integrity": (setup_receipt_integrity, receipt_transcript("identity_core_apply")),
    "error-proposal-integrity": (setup_proposal_integrity, receipt_transcript("identity_core_apply")),
    "error-proposal-decided": (setup_proposal_decided, receipt_transcript("identity_core_apply")),
    "error-stale-authority": (setup_stale, receipt_transcript("identity_core_apply")),
    "legacy-v4": (setup_v4, v4_reads_transcript),
    "foreign-store": (lambda path: setup_incompatible(path, future=False), mutation_error_transcript),
    "future-store": (lambda path: setup_incompatible(path, future=True), mutation_error_transcript),
    "wal-sidecar": (setup_wal_sidecar, mutation_error_transcript),
    "work-store-error": (setup_work_error, work_error_transcript),
    "work-env": (setup_work_env, work_env_transcript),
    "work-env-precedence": (setup_work_precedence, work_precedence_transcript),
    "work-env-blank": (setup_work_blank_env, work_precedence_transcript),
}

UNCHANGED_AFTER_CALL = {
    "error-proposal-decided",
    "error-proposal-integrity",
    "error-receipt-integrity",
    "error-stale-authority",
    "foreign-store",
    "future-store",
    "legacy-v4",
    "wal-sidecar",
}


def write_sitecustomize(path: Path) -> None:
    path.write_text(
        """from datetime import datetime as Base, timedelta, timezone
START = Base(2026, 9, 30, tzinfo=timezone.utc)
class Seconds(Base):
    calls = 0
    @classmethod
    def now(cls, tz=None):
        result = START + timedelta(seconds=cls.calls); cls.calls += 1
        return result if tz is None else result.astimezone(tz)
class Micros(Base):
    calls = 0
    @classmethod
    def now(cls, tz=None):
        result = START + timedelta(microseconds=cls.calls + 1); cls.calls += 1
        return result if tz is None else result.astimezone(tz)
import memory_core.store as store
import memory_core.packet as packet
import trajecta_identity.identity as identity
import trajecta_identity.activation as activation
store.datetime = Seconds
packet.datetime = Seconds
identity.datetime = Seconds
activation.datetime = Micros
""",
        encoding="utf-8",
    )


def assert_contained(work: Path, path: Path) -> None:
    target = path if path.is_absolute() else work / path
    if not target.resolve().is_relative_to(work.resolve()):
        raise AssertionError("writable path escapes MCP fixture")


def isolated_environment(work: Path, clock: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join((str(clock), str(ROOT))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "0"),
    }
    if sys.platform == "win32":
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ:
                environment[key] = os.environ[key]
    for key, value in (extra or {}).items():
        if key != "TRAJECTA_WORK_ROOT":
            raise AssertionError(f"unapproved child env: {key}")
        environment[key] = value
    for key, directory in {
        "HOME": "home", "USERPROFILE": "home", "XDG_DATA_HOME": "xdg",
        "LOCALAPPDATA": "local", "APPDATA": "app",
        "TRAJECTA_IDENTITY_DATA_DIR": "data", "TRAJECTA_IDENTITY_PROFILES": "profiles",
        "TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp",
    }.items():
        path = work / directory
        assert_contained(work, path)
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    shutil.copytree(BUNDLED_PROFILES, Path(environment["TRAJECTA_IDENTITY_PROFILES"]), dirs_exist_ok=True)
    return environment


def assert_startup_paths(work: Path, environment: dict[str, str], profile: str, database: str) -> None:
    path = Path(profile)
    if not path.is_absolute():
        path = work / path
    manifest = path if path.suffix == ".json" else path / "profile.json"
    if not manifest.is_file():
        manifest = Path(environment["TRAJECTA_IDENTITY_PROFILES"]) / profile / "profile.json"
    name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
    assert_contained(work, Path(database))
    assert_contained(work, profile_db(name, env=environment))
    data = Path(environment["TRAJECTA_IDENTITY_DATA_DIR"])
    assert_contained(work, data / "profiles" / safe_fs_name(name))
    assert_contained(work, data / "profiles" / safe_fs_name(name) / "profile.json")
    assert_contained(work, data / ".last-profile")
    if environment.get("TRAJECTA_WORK_ROOT", "").strip():
        assert_contained(work, Path(environment["TRAJECTA_WORK_ROOT"].strip()))


def run_oracle(work: Path, transcript: bytes, profile: str, extra_env: dict[str, str] | None = None) -> tuple[bytes, bytes]:
    from tools.r3.busy import held_writer
    if work.name == "p16-store-busy":
        # Run exactly the same production subprocess while the test-only holder pauses.
        clock = work / "holder-clock"
        clock.mkdir()
        env = isolated_environment(work, clock, extra_env)
        with held_writer("py", work / "store.sqlite3", env):
            before = (work / "store.sqlite3").read_bytes()
            import time
            start = time.monotonic()
            result = _run_oracle(work, transcript, profile, extra_env)
            assert time.monotonic() - start >= 4.5
            assert b"StoreBusy: store is busy; retry later" in result[0] and not result[1]
            assert (work / "store.sqlite3").read_bytes() == before
        return result
    return _run_oracle(work, transcript, profile, extra_env)


def _run_oracle(work: Path, transcript: bytes, profile: str, extra_env: dict[str, str] | None = None) -> tuple[bytes, bytes]:
    clock = work / "clock"
    clock.mkdir()
    write_sitecustomize(clock / "sitecustomize.py")
    environment = isolated_environment(work, clock, extra_env)
    assert_startup_paths(work, environment, profile, "store.sqlite3")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "trajecta_identity.mcp_server",
            "--profile",
            profile,
            "--db",
            "store.sqlite3",
        ],
        cwd=work,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(transcript)
    process.stdin.close()
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"oracle exited {return_code}: {stderr.decode(errors='replace')}")
    return stdout, stderr


def portable_dump(path: Path) -> dict[str, Any]:
    if path.with_name(path.name + "-wal").exists():
        with tempfile.TemporaryDirectory(prefix="trajecta-mcp-dump-") as temporary:
            copy = Path(temporary) / "store.sqlite3"
            shutil.copyfile(path, copy)
            return dump_database(copy)
    return dump_database(path)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def generate(output: Path) -> None:
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("MCP corpus generation requires Python 3.11 / Unicode 14.0.0")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="trajecta-golden-mcp-") as temporary:
        workspace = Path(temporary)
        for name, (setup, transcript_builder) in sorted(SCENARIOS.items()):
            scenario = output / name
            scenario.mkdir()
            work = workspace / name
            work.mkdir()
            store = work / "store.sqlite3"
            with clock_context() if setup else nullcontext():
                state = setup(store) if setup else {}
            profile = state.get("profile", "example")
            transcript = transcript_builder(state)
            if state.get("fixture"):
                # Everything the replay needs besides the store: profile, work roots, env.
                shutil.copytree(work, scenario / "fixture")
                write_json(scenario / "invocation.json", {"profile": profile, "env": state.get("env", {})})
            (scenario / "transcript.in").write_bytes(transcript)
            initial_bytes = store.read_bytes() if store.exists() else None
            if store.exists():
                shutil.copyfile(store, scenario / "initial.sqlite3")
            sidecar = store.with_name(store.name + "-wal")
            initial_sidecar = sidecar.read_bytes() if sidecar.exists() else None
            if sidecar.exists():
                shutil.copyfile(sidecar, scenario / "initial.sqlite3-wal")
            expected, stderr = run_oracle(work, transcript, profile, state.get("env"))
            if stderr:
                raise AssertionError(f"unexpected oracle stderr for {name}: {stderr.decode(errors='replace')}")
            (scenario / "expected.out").write_bytes(expected)
            if name in UNCHANGED_AFTER_CALL:
                if not store.exists() or store.read_bytes() != initial_bytes:
                    raise AssertionError(f"{name} changed store bytes after refusal")
                if (sidecar.read_bytes() if sidecar.exists() else None) != initial_sidecar:
                    raise AssertionError(f"{name} changed sidecar bytes after refusal")
            if store.exists():
                shutil.copyfile(store, scenario / "store.sqlite3")
                write_json(scenario / "dump.json", portable_dump(store))
                if sidecar.exists():
                    shutil.copyfile(sidecar, scenario / "store.sqlite3-wal")
            else:
                (scenario / "absent").touch()

    source_paths = sorted({path for pattern in SOURCES for path in ROOT.glob(pattern)})
    source_hashes = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
    }
    corpus = sorted(path for path in output.rglob("*") if path.is_file())
    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in corpus}
    for table in sorted(TABLES.glob("*.json")):
        files[f"tables/{table.name}"] = hashlib.sha256(table.read_bytes()).hexdigest()
    manifest = {
        "schema": "trajecta.golden-mcp-manifest/v1",
        "oracle_commit": ORACLE_COMMIT,
        "oracle_semantics": ORACLE_SEMANTICS,
        "oracle_sources": source_hashes,
        "python": sys.version.split()[0],
        "unicode": unicodedata.unidata_version,
        "sqlite": sqlite3.sqlite_version,
        "files": dict(sorted(files.items())),
    }
    write_json(output / "MANIFEST.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "spec" / "golden-mcp-v1")
    generate(parser.parse_args().output)
